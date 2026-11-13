"""Regenerates the reference-board scripts in this folder (17-part ESP32-C3 dev board, offline parts only).

    python tests/budget/gen.py

c3_legacy    every single-step tool, images switched off explicitly, one small render at the end
c3_defaults  the long flow with NO image flags at all (what a model does when it follows old habits)
c3_short     the short path: schematic, then board_build, then the fab pack
"""
import json
from pathlib import Path

H = Path(__file__).resolve().parent


def P(**k):
    return dict(op="place", **k)


def C(net, *pins):
    return {"op": "connect", "pins": list(pins), "net": net}


def S(tool, stage, **a):
    return {"tool": tool, "stage": stage, "args": a}


SW = dict(lib_id="Switch:SW_Push", footprint="Button_Switch_SMD:SW_SPST_TL3342", fields={"LCSC": "C318884"})
OPS = [
    P(lib_id="Connector:USB_C_Receptacle_USB2.0_16P", id="usb", group="connector",
      footprint="Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12", fields={"LCSC": "C165948"}),
    P(spec="5.1k 0402", id="rcc1", near="usb"), P(spec="5.1k 0402", id="rcc2", near="usb"),
    P(lib_id="Regulator_Linear:AMS1117-3.3", id="ldo", group="power", fields={"LCSC": "C6186"}),
    P(spec="10uF 0603", id="cin", near="ldo"), P(spec="22uF 0805", id="cout", near="ldo"),
    P(lib_id="RF_Module:ESP32-C3-WROOM-02", id="mcu", group="mcu", fields={"LCSC": "C2934560"}),
    P(spec="100nF 0402", id="c1", near="mcu"), P(spec="10uF 0603", id="c2", near="mcu"),
    P(spec="10k 0402", id="ren", near="mcu"), P(spec="1uF 0402", id="cen", near="mcu"),
    P(id="swrst", **SW), P(spec="10k 0402", id="rboot", near="mcu"), P(id="swboot", **SW),
    P(spec="LED blue 0603", id="led"), P(spec="1k 0402", id="rled", near="led"),
    P(lib_id="Connector_Generic:Conn_01x10", id="hdr", group="connector",
      footprint="Connector_PinHeader_2.54mm:PinHeader_1x10_P2.54mm_Vertical", fields={"LCSC": "C57369"}),
    C("VBUS", "usb.VBUS", "ldo.VI", "cin.1"),
    C("GND", "usb.GND", "usb.SHIELD", "rcc1.2", "rcc2.2", "ldo.GND", "cin.2", "cout.2", "mcu.GND", "c1.2", "c2.2",
      "cen.2", "swrst.2", "swboot.2", "hdr.2"),
    C("+3V3", "ldo.VO", "cout.1", "mcu.3V3", "c1.1", "c2.1", "ren.1", "rboot.1", "led.A", "hdr.1"),
    C("CC1", "usb.CC1", "rcc1.1"), C("CC2", "usb.CC2", "rcc2.1"),
    C("USB_DP", "usb.D+", "mcu.IO19"), C("USB_DN", "usb.D-", "mcu.IO18"),
    C("EN", "mcu.EN", "ren.2", "cen.1", "swrst.1"), C("BOOT", "mcu.IO9", "rboot.2", "swboot.1"),
    C("LED_K", "led.K", "rled.1"), C("LED", "rled.2", "mcu.IO8"),
    C("TX", "hdr.3", "mcu.IO21/TXD"), C("RX", "hdr.4", "mcu.IO20/RXD"),
] + [C(f"IO{n}", f"hdr.{n + 3}", f"mcu.IO{n}") for n in range(2, 8)] + [
    {"op": "no_connect", "pins": ["usb.SBU1", "usb.SBU2", "mcu.IO0", "mcu.IO1", "mcu.IO10"]},
    {"op": "pwr_flag", "net": "VBUS"}, {"op": "pwr_flag", "net": "GND"},
]
CREATE = S("project_create", "0 orient", name="c3ref", dir="$DIR", template="jlc-2layer", board_size=[60, 36])
FAB = S("fab_export", "5 fab", waive=["all"])
STATUS = S("copper_status", "6 status")

legacy = [CREATE,
          S("sch_apply", "2 schematic", ops=OPS, preview=False), S("sch_tidy", "2 schematic", preview=False),
          S("design_review", "2 schematic"),
          S("pcb_sync", "3 board", render=False), S("pcb_setup", "3 board", width=60, height=36, radius=1.5, render=False),
          S("pcb_place", "3 board", auto=True, render=False), S("pcb_route_guided", "3 board", render=False),
          S("pcb_pour", "3 board", net="GND", stitch=True, render=False),
          S("verify", "4 verify"), S("render", "4 verify", target="pcb", width=800), FAB, STATUS]
defaults = [S("copper_status", "0 orient"), CREATE,
            S("sch_apply", "2 schematic", ops=OPS), S("sch_tidy", "2 schematic"), S("verify", "2 schematic"),
            S("render", "2 schematic", target="schematic"), S("design_review", "2 schematic"),
            S("pcb_sync", "3 board"), S("pcb_setup", "3 board", width=60, height=36, radius=1.5),
            S("pcb_place", "3 board", auto=True), S("pcb_route_guided", "3 board"),
            S("pcb_pour", "3 board", net="GND", stitch=True),
            S("verify", "4 verify"), S("render", "4 verify", target="pcb"), S("render", "4 verify", target="pcb3d"),
            S("pcb_read", "4 verify"), FAB, STATUS]
short = [CREATE,
         S("sch_apply", "2 schematic", ops=OPS), S("sch_tidy", "2 schematic"),
         S("board_build", "3 board", width=60, height=36, radius=1.5, router="guided"), FAB, STATUS]

if __name__ == "__main__":
    for name, s in (("c3_legacy", legacy), ("c3_defaults", defaults), ("c3_short", short)):
        (H / f"{name}.json").write_text(json.dumps(s, indent=1) + "\n", encoding="utf-8")
    print("ops bytes", len(json.dumps(OPS, separators=(",", ":"))))
