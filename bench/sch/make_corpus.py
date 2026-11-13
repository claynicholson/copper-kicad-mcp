"""Build / refresh the bench corpus (bench/sch/corpus).

    python bench/sch/make_corpus.py [--force] [--external <project dir>]

* copies the four fixtures of tests/data/pro
* copies an external project when --external <dir> is given (schematic sheets, project file, symbol tables and
  project symbol libraries only; the source is only read, never modified)
* constructs the unseen designs of bench/sch/corpus_unseen (a 555 blinker, an RS-485 node, an LM386 amplifier) and
  two corpus designs with the package's own sch_apply (layout="keep": the raw label-style drawing an agent
  gets before any tidy): an op-amp analog front end and a motor driver.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

_env.setup()
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.project import Project  # noqa: E402
import copper_kicad.tools  # noqa: E402,F401


def _p(lib, id_, value=None, near=None):
    d = {"op": "place", "lib_id": lib, "id": id_}
    if value:
        d["value"] = value
    if near:
        d["near"] = near
    return d


def _c(net, *pins):
    return {"op": "connect", "pins": list(pins), "net": net}


AFE = [
    _p("Connector_Generic:Conn_01x02", "jpwr", "PWR_IN"), _p("Regulator_Linear:L7805", "reg"),
    _p("Device:C", "cin", "330n", "reg"), _p("Device:C", "cout", "100n", "reg"),
    _p("Reference_Voltage:LM4040DBZ-2.5", "vref"), _p("Device:R", "rref", "1k", "vref"), _p("Device:C", "cref", "100n", "vref"),
    _p("Connector_Generic:Conn_01x03", "jsens", "SENSOR"), _p("Device:D_TVS", "tvs", "SMBJ5.0A", "jsens"),
    _p("Device:R", "rin", "10k"), _p("Device:C", "cinf", "10n"),
    _p("Amplifier_Operational:LM358", "amp"), _p("Device:C", "camp", "100n", "amp"),
    _p("Device:R", "rf", "100k", "amp"), _p("Device:R", "rg", "10k", "amp"),
    _p("Device:R", "rlp", "4k7", "amp"), _p("Device:C", "clp", "33n", "amp"),
    _p("Analog_ADC:MCP3201", "adc"), _p("Device:C", "cadc", "100n", "adc"), _p("Device:R", "rcs", "10k", "adc"),
    _p("Connector_Generic:Conn_01x06", "jspi", "SPI"),
    _c("+12V", "jpwr.1", "reg.VI", "cin.1"),
    _c("GND", "jpwr.2", "reg.GND", "cin.2", "cout.2", "vref.2", "cref.2", "jsens.3", "tvs.2", "cinf.2", "amp.4", "camp.2",
       "rg.2", "clp.2", "adc.3", "adc.4", "cadc.2", "jspi.2"),
    _c("+5V", "reg.VO", "cout.1", "rref.1", "jsens.1", "amp.8", "camp.1", "adc.8", "cadc.1", "rcs.1", "jspi.1"),
    _c("VREF", "rref.2", "vref.1", "cref.1", "adc.1"),
    _c("SENSOR_IN", "jsens.2", "tvs.1", "rin.1"),
    _c("AIN_F", "rin.2", "cinf.1", "amp.3"),
    _c("FB", "amp.2", "rf.1", "rg.1"),
    _c("AMP_OUT", "amp.1", "rf.2", "rlp.1"),
    _c("LPF", "rlp.2", "clp.1", "amp.5"),
    _c("ADC_IN", "amp.6", "amp.7", "adc.2"),
    _c("~{CS}", "adc.5", "rcs.2", "jspi.3"), _c("SCK", "adc.7", "jspi.4"), _c("MISO", "adc.6", "jspi.5"),
    {"op": "no_connect", "pins": ["jspi.6", "vref.3"]},
    {"op": "pwr_flag", "net": "+12V"}, {"op": "pwr_flag", "net": "GND"}, {"op": "pwr_flag", "net": "VREF"},
]

MOTOR = [
    _p("Connector:Screw_Terminal_01x02", "jin", "VIN 12V"), _p("Device:Fuse", "f", "2A"),
    _p("Device:D_TVS", "tvs", "SMBJ15A"), _p("Device:C_Polarized", "cbulk", "100u"),
    _p("Regulator_Switching:LM2596S-5", "buck"), _p("Device:L", "l", "33u", "buck"),
    _p("Device:D_Schottky", "dsw", "SS34", "buck"), _p("Device:C_Polarized", "cout", "220u", "buck"),
    _p("MCU_Microchip_ATtiny:ATtiny85-20S", "mcu"), _p("Device:C", "cmcu", "100n", "mcu"),
    _p("Device:R", "rrst", "10k", "mcu"), _p("Switch:SW_Push", "sw", "RESET", "mcu"),
    _p("Driver_Motor:DRV8833PW", "drv"), _p("Device:C", "cvm", "10u", "drv"), _p("Device:C", "cvint", "2u2", "drv"),
    _p("Device:C", "cvcp", "10n", "drv"), _p("Device:R", "rslp", "10k", "drv"), _p("Device:R", "rflt", "10k", "drv"),
    _p("Device:R", "rsa", "0R2", "drv"), _p("Device:R", "rsb", "0R2", "drv"),
    _p("Connector:Screw_Terminal_01x02", "jma", "MOTOR A"), _p("Connector:Screw_Terminal_01x02", "jmb", "MOTOR B"),
    _p("Device:R", "rled", "1k"), _p("Device:LED", "led", "PWR"),
    _p("Connector_Generic:Conn_01x06", "jprog", "PROG"),
    _c("VIN_RAW", "jin.1", "f.1"),
    _c("+12V", "f.2", "tvs.1", "cbulk.1", "buck.1", "drv.12", "cvm.1", "cvcp.2"),
    _c("GND", "jin.2", "tvs.2", "cbulk.2", "buck.3", "buck.5", "dsw.2", "cout.2", "mcu.4", "cmcu.2", "sw.2", "drv.13",
       "cvm.2", "cvint.2", "rsa.2", "rsb.2", "led.1", "jprog.2"),
    _c("SW", "buck.2", "l.1", "dsw.1"),
    _c("+5V", "l.2", "buck.4", "cout.1", "mcu.8", "cmcu.1", "rrst.1", "rslp.1", "rflt.1", "rled.1", "jprog.1"),
    _c("LED_A", "rled.2", "led.2"),
    _c("~{RESET}", "mcu.1", "rrst.2", "sw.1", "jprog.3"),
    _c("AIN1", "mcu.5", "drv.16", "jprog.4"), _c("AIN2", "mcu.6", "drv.15", "jprog.5"),
    _c("BIN1", "mcu.7", "drv.9", "jprog.6"), _c("BIN2", "mcu.2", "drv.10"),
    _c("~{FAULT}", "mcu.3", "drv.8", "rflt.2"), _c("~{SLEEP}", "drv.1", "rslp.2"),
    _c("VINT", "drv.14", "cvint.1"), _c("VCP", "drv.11", "cvcp.1"),
    _c("AISEN", "drv.3", "rsa.1"), _c("BISEN", "drv.6", "rsb.1"),
    _c("AOUT1", "drv.2", "jma.1"), _c("AOUT2", "drv.4", "jma.2"),
    _c("BOUT1", "drv.7", "jmb.1"), _c("BOUT2", "drv.5", "jmb.2"),
    {"op": "pwr_flag", "net": "VIN_RAW"}, {"op": "pwr_flag", "net": "GND"}, {"op": "pwr_flag", "net": "+12V"},
    {"op": "pwr_flag", "net": "+5V"}, {"op": "pwr_flag", "net": "VINT"},
]

BUILT = {"opamp-afe": AFE, "motor-driver": MOTOR}

# ── designs the engine was NOT developed against (bench/sch/corpus_unseen, reported separately by run.py) ──
BLINKER = [
    _p("Connector:Barrel_Jack", "jin", "9V DC"), _p("Device:D", "drev", "1N5819"), _p("Device:C_Polarized", "cbulk", "100u"),
    _p("Timer:NE555P", "tmr"), _p("Device:C", "cdec", "100n", "tmr"), _p("Device:R", "ra", "10k", "tmr"),
    _p("Device:R", "rb", "68k", "tmr"), _p("Device:C_Polarized", "ct", "10u", "tmr"), _p("Device:C", "ccv", "10n", "tmr"),
    _p("Device:R", "rled", "470"), _p("Device:LED", "led", "RED"),
    _p("Connector_Generic:Conn_01x02", "jout", "PULSE OUT"),
    _c("VIN_RAW", "jin.1", "drev.2"),
    _c("+9V", "drev.1", "cbulk.1", "tmr.8", "tmr.4", "cdec.1", "ra.1"),
    _c("GND", "jin.2", "cbulk.2", "tmr.1", "cdec.2", "ct.2", "ccv.2", "led.1", "jout.2"),
    _c("DISCH", "ra.2", "rb.1", "tmr.7"),
    _c("TIMING", "rb.2", "ct.1", "tmr.2", "tmr.6"),
    _c("CV", "tmr.5", "ccv.1"),
    _c("PULSE", "tmr.3", "rled.1", "jout.1"),
    _c("LED_A", "rled.2", "led.2"),
    {"op": "pwr_flag", "net": "VIN_RAW"}, {"op": "pwr_flag", "net": "GND"}, {"op": "pwr_flag", "net": "+9V"},
]

RS485 = [
    _p("Connector:Screw_Terminal_01x02", "jpwr", "12V IN"), _p("Regulator_Linear:L7805", "reg"),
    _p("Device:C", "cin", "330n", "reg"), _p("Device:C", "cout", "100n", "reg"),
    _p("MCU_Microchip_ATmega:ATmega328P-A", "mcu"), _p("Device:C", "c1", "100n", "mcu"), _p("Device:C", "c2", "100n", "mcu"),
    _p("Device:C", "caref", "100n", "mcu"), _p("Device:Crystal", "y", "16MHz", "mcu"),
    _p("Device:C", "cx1", "22p", "mcu"), _p("Device:C", "cx2", "22p", "mcu"), _p("Device:R", "rrst", "10k", "mcu"),
    _p("Device:R", "rled", "1k", "mcu"), _p("Device:LED", "led", "STATUS", "mcu"),
    _p("Interface_UART:MAX485E", "xcvr"), _p("Device:C", "cx", "100n", "xcvr"), _p("Device:R", "rterm", "120", "xcvr"),
    _p("Connector:Screw_Terminal_01x03", "jbus", "RS485"),
    _p("Sensor_Temperature:LM75B", "tmp"), _p("Device:C", "ctmp", "100n", "tmp"),
    _p("Device:R", "rsda", "4k7"), _p("Device:R", "rscl", "4k7"),
    _p("Connector_Generic:Conn_01x06", "jisp", "ISP"),
    _c("+12V", "jpwr.1", "reg.VI", "cin.1"),
    _c("GND", "jpwr.2", "reg.GND", "cin.2", "cout.2", "mcu.3", "mcu.5", "mcu.21", "c1.2", "c2.2", "caref.2", "cx1.2", "cx2.2",
       "led.1", "xcvr.5", "cx.2", "jbus.3", "tmp.4", "tmp.5", "tmp.6", "tmp.7", "ctmp.2", "jisp.6"),
    _c("+5V", "reg.VO", "cout.1", "mcu.4", "mcu.6", "mcu.18", "c1.1", "c2.1", "rrst.1", "xcvr.8", "cx.1", "tmp.8", "ctmp.1",
       "rsda.1", "rscl.1", "jisp.2"),
    _c("AREF", "mcu.20", "caref.1"),
    _c("XTAL1", "mcu.7", "y.1", "cx1.1"), _c("XTAL2", "mcu.8", "y.2", "cx2.1"),
    _c("~{RESET}", "mcu.29", "rrst.2", "jisp.5"),
    _c("MOSI", "mcu.15", "jisp.4"), _c("MISO", "mcu.16", "jisp.1"), _c("SCK", "mcu.17", "jisp.3"),
    _c("RXD", "mcu.30", "xcvr.1"), _c("TXD", "mcu.31", "xcvr.4"), _c("TX_EN", "mcu.32", "xcvr.2", "xcvr.3"),
    _c("RS485_A", "xcvr.6", "rterm.1", "jbus.1"), _c("RS485_B", "xcvr.7", "rterm.2", "jbus.2"),
    _c("SDA", "mcu.27", "tmp.1", "rsda.2"), _c("SCL", "mcu.28", "tmp.2", "rscl.2"),
    _c("TEMP_ALERT", "mcu.1", "tmp.3"),
    _c("LED", "mcu.12", "rled.1"), _c("LED_A", "rled.2", "led.2"),
    {"op": "no_connect", "pins": ["mcu.2", "mcu.9", "mcu.10", "mcu.11", "mcu.13", "mcu.14", "mcu.19", "mcu.22", "mcu.23",
                                  "mcu.24", "mcu.25", "mcu.26"]},
    {"op": "pwr_flag", "net": "+12V"}, {"op": "pwr_flag", "net": "GND"},
]

AUDIO = [
    _p("Connector:Barrel_Jack", "jpwr", "9V DC"), _p("Device:C_Polarized", "cbulk", "470u"), _p("Device:C", "cdec", "100n"),
    _p("Connector_Generic:Conn_01x02", "jin", "LINE IN"), _p("Device:R_Potentiometer", "vol", "10k"),
    _p("Device:C", "cin", "470n"), _p("Amplifier_Audio:LM386", "amp"),
    _p("Device:C_Polarized", "cgain", "10u", "amp"), _p("Device:R", "rgain", "1k2", "amp"),
    _p("Device:C_Polarized", "cbyp", "10u", "amp"), _p("Device:C_Polarized", "cout", "220u", "amp"),
    _p("Device:R", "rz", "10", "amp"), _p("Device:C", "cz", "47n", "amp"),
    _p("Connector_Generic:Conn_01x02", "jspk", "SPEAKER"),
    _c("+9V", "jpwr.1", "cbulk.1", "cdec.1", "amp.6"),
    _c("GND", "jpwr.2", "cbulk.2", "cdec.2", "jin.2", "vol.1", "amp.2", "amp.4", "cbyp.2", "cz.2", "jspk.2"),
    _c("LINE_IN", "jin.1", "vol.3"),
    _c("WIPER", "vol.2", "cin.1"),
    _c("AMP_IN", "cin.2", "amp.3"),
    _c("GAIN_A", "amp.1", "cgain.1"), _c("GAIN_B", "cgain.2", "rgain.1"), _c("GAIN_C", "rgain.2", "amp.8"),
    _c("BYPASS", "amp.7", "cbyp.1"),
    _c("AMP_OUT", "amp.5", "cout.1", "rz.1"),
    _c("ZOBEL", "rz.2", "cz.1"),
    _c("SPK", "cout.2", "jspk.1"),
    {"op": "pwr_flag", "net": "+9V"}, {"op": "pwr_flag", "net": "GND"},
]

UNSEEN = {"ne555-blinker": BLINKER, "rs485-node": RS485, "audio-amp": AUDIO}
FIXTURES = ["c3-devboard", "li-ion-powerbank", "rp2040-min", "stm32-sensor"]


def copy_project(src: Path, dst: Path):
    """Schematic sheets, project file, symbol tables and project-local symbol libraries. Nothing else."""
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.rglob("*"):
        rel = f.relative_to(src)
        if not f.is_file() or f.name.startswith("~") or "copper-out" in rel.parts or ".copper" in rel.parts:
            continue
        if f.suffix in (".kicad_sch", ".kicad_pro", ".kicad_sym") or f.name == "sym-lib-table":
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst / rel)


def build(name: str, ops: list, root: Path | None = None):
    d = (root or _env.CORPUS) / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    pr = Project(d, name)
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "ops": ops, "preview": False, "layout": "keep"})
    if out.get("isError"):
        raise SystemExit(f"{name}: sch_apply failed: {out['content'][0]['text'][:1500]}")
    sc = out["structuredContent"]
    print(name, "built:", {k: sc.get(k) for k in ("connectivity", "warnings", "verify") if sc.get(k)})
    shutil.rmtree(d / ".copper", ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild designs that already exist")
    ap.add_argument("--external", action="append", default=[], help="project folder to copy in (read-only)")
    a = ap.parse_args()
    _env.CORPUS.mkdir(parents=True, exist_ok=True)
    for n in FIXTURES:
        if a.force or not (_env.CORPUS / n).exists():
            shutil.rmtree(_env.CORPUS / n, ignore_errors=True)
            copy_project(_env.ROOT / "tests" / "data" / "pro" / n, _env.CORPUS / n)
            print(n, "copied")
    for e in a.external:
        src = Path(e)
        if a.force or not (_env.CORPUS / src.name).exists():
            shutil.rmtree(_env.CORPUS / src.name, ignore_errors=True)
            copy_project(src, _env.CORPUS / src.name)
            print(src.name, "copied")
    for n, ops in BUILT.items():
        if a.force or not (_env.CORPUS / n).exists():
            build(n, ops)
    for n, ops in UNSEEN.items():
        if a.force or not (_env.UNSEEN / n).exists():
            build(n, ops, _env.UNSEEN)


if __name__ == "__main__":
    main()
