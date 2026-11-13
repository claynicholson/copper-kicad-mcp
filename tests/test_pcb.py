"""PCB engine tests: sync → setup → place → route (Freerouting) → pour, verified
with kicad-cli DRC (+ schematic parity), plus unit tests for placement math,
netlist parsing, rules, explicit routing, edits and the open-in-KiCad guard.

Run:  python -m pytest tests/test_pcb.py      or      python tests/test_pcb.py
Needs KiCad 9/10. Set COPPER_TEST_NO_ROUTE=1 to skip the Freerouting step (it
downloads Java + Freerouting into ~/.copper/tools on first use).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import uuid as _uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Copper's state (active project, recent list) must never land in the real ~/.copper: project.STATE_DIR is
# fixed when copper_kicad is first imported, so this has to come before that import.
__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad import project as projmod, sexpr  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.sexpr import Sym  # noqa: E402
import copper_kicad.tools.pcb_tools  # noqa: E402,F401 — registers the tools
from copper_kicad.pcb import board, place  # noqa: E402

# ── fixture: a small, realistic project (ATtiny85 + AMS1117 + crystal + LED +
#    JST power in + ISP header) built from stock KiCad libraries, written
#    directly as S-expressions so it does not depend on the schematic engine ──
ROOT_UUID = "6d1b0f2e-3c55-4a8e-9a43-1f2c3b4d5e6f"

# ref, lib_id, value, footprint, LCSC, at, {pin: net}
PARTS = [
    ("J1", "Connector_Generic:Conn_01x02", "PWR_IN", "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical", "C131337",
     (30, 40), {"1": "VIN", "2": "GND"}),
    ("U1", "Regulator_Linear:AMS1117-3.3", "AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", "C6186",
     (70, 40), {"1": "GND", "2": "+3V3", "3": "VIN"}),
    ("C1", "Device:C", "10uF", "Capacitor_SMD:C_0805_2012Metric", "C15850", (50, 60), {"1": "VIN", "2": "GND"}),
    ("C2", "Device:C", "10uF", "Capacitor_SMD:C_0805_2012Metric", "C15850", (90, 60), {"1": "+3V3", "2": "GND"}),
    ("U2", "MCU_Microchip_ATtiny:ATtiny85-20S", "ATtiny85-20S", "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm", "C89852",
     (150, 70), {"1": "RESET", "2": "XTAL1", "3": "XTAL2", "4": "GND", "5": "MOSI", "6": "MISO", "7": "SCK", "8": "+3V3"}),
    ("C3", "Device:C", "100nF", "Capacitor_SMD:C_0603_1608Metric", "C14663", (110, 60), {"1": "+3V3", "2": "GND"}),
    ("Y1", "Device:Crystal_GND24", "8MHz", "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm", "C115962", (190, 110),
     {"1": "XTAL1", "3": "XTAL2", "2": "GND", "4": "GND"}),
    ("C4", "Device:C", "22pF", "Capacitor_SMD:C_0603_1608Metric", "C1653", (170, 130), {"1": "XTAL1", "2": "GND"}),
    ("C5", "Device:C", "22pF", "Capacitor_SMD:C_0603_1608Metric", "C1653", (210, 130), {"1": "XTAL2", "2": "GND"}),
    ("R1", "Device:R", "1k", "Resistor_SMD:R_0603_1608Metric", "C21190", (190, 40), {"1": "MISO", "2": "LED_A"}),
    ("D1", "Device:LED", "RED", "LED_SMD:LED_0603_1608Metric", "C2286", (220, 40), {"2": "LED_A", "1": "GND"}),
    ("R2", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "C25804", (110, 100), {"1": "+3V3", "2": "RESET"}),
    ("J2", "Connector_Generic:Conn_02x03_Odd_Even", "ISP", "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical",
     "C65114", (250, 90), {"1": "MISO", "2": "+3V3", "3": "SCK", "4": "MOSI", "5": "RESET", "6": "GND"}),
]


def _u():
    return str(_uuid.uuid4())


def _symbol_def(lib_id: str, project_dir: Path):
    nick, name = lib_id.split(":")
    lib = env.find_lib("sym", nick, project_dir)
    p = Path(lib.path)
    if p.is_dir():
        p = p / f"{name}.kicad_sym"
    tree = sexpr.loads(p.read_text(encoding="utf-8"))
    syms = {s[1]: s for s in sexpr.find_all(tree, "symbol")}
    s = copy.deepcopy(syms[name])
    ext = sexpr.value(s, "extends")
    if ext:
        parent = copy.deepcopy(syms[str(ext)])
        child_props = {p[1]: p for p in sexpr.find_all(s, "property")}
        flat = [parent[0], name]
        for e in parent[2:]:
            if isinstance(e, list) and e and e[0] == "property" and e[1] in child_props:
                flat.append(child_props.pop(e[1]))
            elif isinstance(e, list) and e and e[0] == "symbol":
                e[1] = str(e[1]).replace(str(ext), name, 1)
                flat.append(e)
            else:
                flat.append(e)
        for e in child_props.values():
            flat.insert(2, e)
        s = flat
    pins = {}

    def walk(x):
        for e in x:
            if isinstance(e, list) and e:
                if e[0] == "pin":
                    at = sexpr.find(e, "at")
                    pins.setdefault(str(sexpr.value(e, "number")), (float(at[1]), float(at[2])))
                else:
                    walk(e)
    walk(s)
    s[1] = lib_id
    return s, pins


def _font():
    return [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]]]


def _prop(name, val, at, hide=False):
    p = [Sym("property"), name, val, [Sym("at"), at[0], at[1], 0], _font()]
    if hide:
        p.insert(4, [Sym("hide"), Sym("yes")])
    return p


def build(dest: Path, name: str = "copper_demo", parts=PARTS) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    defs, pinmap = {}, {}
    for ref, lib_id, *_ in parts:
        if lib_id not in defs:
            defs[lib_id], pinmap[lib_id] = _symbol_def(lib_id, dest)
    sch = [Sym("kicad_sch"), [Sym("version"), 20260206], [Sym("generator"), "eeschema"],
           [Sym("generator_version"), "10.0"], [Sym("uuid"), ROOT_UUID], [Sym("paper"), "A4"],
           [Sym("lib_symbols")] + list(defs.values())]
    for ref, lib_id, value, fpid, lcsc, (sx, sy), conns in parts:
        su = _u()
        inst = [Sym("symbol"), [Sym("lib_id"), lib_id], [Sym("at"), sx, sy, 0], [Sym("unit"), 1],
                [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")], [Sym("on_board"), Sym("yes")],
                [Sym("dnp"), Sym("no")], [Sym("uuid"), su],
                _prop("Reference", ref, (sx, sy - 8)), _prop("Value", value, (sx, sy + 8)),
                _prop("Footprint", fpid, (sx, sy), True), _prop("Datasheet", "~", (sx, sy), True),
                _prop("LCSC", lcsc, (sx, sy), True)]
        for pn in pinmap[lib_id]:
            inst.append([Sym("pin"), pn, [Sym("uuid"), _u()]])
        inst.append([Sym("instances"), [Sym("project"), name, [Sym("path"), "/" + ROOT_UUID,
                                                                 [Sym("reference"), ref], [Sym("unit"), 1]]]])
        sch.append(inst)
        done = set()
        for pn, net in conns.items():
            px, py = pinmap[lib_id][pn]
            ax, ay = round(sx + px, 2), round(sy - py, 2)
            if (ax, ay) in done:
                continue
            done.add((ax, ay))
            sch.append([Sym("global_label"), net, [Sym("shape"), Sym("passive")], [Sym("at"), ax, ay, 0],
                        [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]], [Sym("justify"), Sym("left")]],
                        [Sym("uuid"), _u()]])
    sch.append([Sym("sheet_instances"), [Sym("path"), "/", [Sym("page"), "1"]]])
    (dest / f"{name}.kicad_sch").write_text(sexpr.dumps(sch) + "\n", encoding="utf-8")
    pro = dest / f"{name}.kicad_pro"
    if not pro.exists():
        pro.write_text(json.dumps({"meta": {"filename": pro.name, "version": 3}, "board": {"design_settings": {}},
                                   "net_settings": {"classes": [], "meta": {"version": 5}}, "sheets": [[ROOT_UUID, "Root"]]},
                                  indent=2), encoding="utf-8")
    return dest


# ── helpers ───────────────────────────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp(prefix="copper-test-pcb-"))


def _project(name):
    d = _TMP / name
    if d.exists():
        shutil.rmtree(d)
    build(d)
    return str(d)


def call(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    if r["isError"]:
        raise AssertionError(f"{name} failed: {r['content'][0]['text'][:800]}")
    return r.get("structuredContent") or {}, r


def call_err(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    assert r["isError"], f"{name} was expected to fail"
    return r["structuredContent"]


def kicad_drc(pcb: Path) -> dict:
    out = Path(tempfile.mkdtemp()) / "drc.json"
    env.run(["pcb", "drc", "--format", "json", "--schematic-parity", "--severity-all", "-o", str(out), str(pcb)])
    d = json.loads(out.read_text(encoding="utf-8"))
    return {"errors": [v for v in d["violations"] if v["severity"] == "error"],
            "unconnected": len(d["unconnected_items"]), "parity": len(d["schematic_parity"])}


# ── pure unit tests ───────────────────────────────────────────────────────
def test_place_math():
    assert place.tf(1, 0, 90) == (0, -1)          # KiCad: +90° is CCW on screen (y down)
    assert place.tf(-0.825, 0, 90) == (0, 0.825)  # matches pcbnew pad positions (checked against SWIG)
    assert place.tf(1, 2, 180) == (-1, -2)
    assert abs(place.cap_farads("100nF") - 1e-7) < 1e-15
    assert abs(place.cap_farads("4u7") - 4.7e-6) < 1e-12
    assert abs(place.cap_farads("22pF") - 22e-12) < 1e-18
    assert place.is_ground("GND") and place.is_ground("/AGND") and not place.is_ground("+3V3")
    assert place.is_power("+3V3") and place.is_power("VBUS") and not place.is_power("SDA")
    assert abs(place.mst_length([(0, 0), (3, 0), (3, 4)]) - 7) < 1e-9


def test_rules_dru_block_is_idempotent():
    from copper_kicad.pcb import rules
    d = Path(_project("rules"))
    pr = projmod.resolve(str(d))
    rules.apply(pr, "jlc")
    rules.apply(pr, "jlc")
    txt = (d / "copper_demo.kicad_dru").read_text(encoding="utf-8")
    assert txt.count(rules.BEGIN) == 1 and txt.startswith("(version 1)")
    pro = json.loads(pr.pro.read_text(encoding="utf-8"))
    names = [c["name"] for c in pro["net_settings"]["classes"]]
    assert names.count("Default") == 1 and names.count("Power") == 1
    assert pro["board"]["design_settings"]["rules"]["min_track_width"] == 0.127


def test_netlist_parse():
    pr = projmod.resolve(_project("netlist"))
    nl = board.netlist(pr)
    refs = {c["ref"]: c for c in nl["components"]}
    assert len(refs) == len(PARTS)
    assert refs["U2"]["fields"]["LCSC"] == "C89852"
    assert refs["U2"]["path"].startswith("/") and len(refs["U2"]["path"]) > 30
    nets = {n["name"]: {tuple(x) for x in n["nodes"]} for n in nl["nets"]}
    assert ("U1", "2") in nets["+3V3"] and ("C3", "1") in nets["+3V3"]
    assert ("Y1", "2") in nets["GND"] and ("Y1", "4") in nets["GND"]


# ── end-to-end ────────────────────────────────────────────────────────────
def test_full_flow():
    d = _project("e2e")
    pcb = Path(d) / "copper_demo.kicad_pcb"
    s, _ = call("pcb_sync", project=d, render=False)
    assert s["added"] == len(PARTS) and not s["errors"] and s["verify"]["drc"]["parity"] == 0
    s2, _ = call("pcb_sync", project=d, render=False)            # idempotent
    assert s2["added"] == 0 and s2["updated"] == 0 and s2["changed"] == []
    s, _ = call("pcb_setup", project=d, width=40, height=30, holes="M3", render=False)
    assert s["outline"]["width"] == 40 and s["outline"]["height"] == 30
    call("pcb_setup", project=d, width=40, height=30, holes="M3", render=False)   # re-run: no duplicates
    rd, _ = call("pcb_read", project=d, what=["footprints"])
    assert sum(1 for f in rd["footprints"] if f["ref"].startswith("H")) == 4
    s, r = call("pcb_place", project=d, auto=True)
    assert s["verify"]["courtyard_overlaps"] == 0
    assert s["score"]["ratsnest_mm_after"] < s["score"]["ratsnest_mm_before"]
    assert any(c["type"] == "image" for c in r["content"])
    rd, _ = call("pcb_read", project=d, what=["footprints"])
    x0, y0, x1, y1 = rd["outline"]["bbox"]
    for f in rd["footprints"]:
        b = f["bbox"]
        assert x0 <= b[0] and b[2] <= x1 and y0 <= b[1] and b[3] <= y1, f"{f['ref']} outside the board"
    by = {f["ref"]: f for f in rd["footprints"]}
    # decoupling cap within a few mm of the MCU supply pin it serves (pad to pin: the placer may rotate the
    # MCU, so centre-to-centre distance says little); power-input connector on the left edge
    pads, _ = call("pcb_read", project=d, what=["pads"], refs=["C3", "U2"], net="+3V3")
    pp = {f["ref"]: f["pad_list"][0] for f in pads["footprints"]}
    assert abs(pp["C3"]["x"] - pp["U2"]["x"]) + abs(pp["C3"]["y"] - pp["U2"]["y"]) < 5, pp
    assert by["J1"]["bbox"][0] - x0 < 1.5
    if os.environ.get("COPPER_TEST_NO_ROUTE"):
        return
    s, _ = call("pcb_route", project=d, render=False)
    assert s["unrouted_before"] > 0
    # Freerouting occasionally leaves one ground link (depends on DSN item order); the pour joins it
    assert s["unrouted_after"] == 0 or set(s.get("unrouted_nets", [])) <= {"GND"}, s
    s, _ = call("pcb_pour", project=d, net="GND", stitch=True, render=False)
    assert s["unrouted"] == 0
    drc = kicad_drc(pcb)
    assert drc["unconnected"] == 0 and drc["parity"] == 0, drc
    assert not drc["errors"], [e["description"] for e in drc["errors"]][:5]
    from copper_kicad import render
    im = render.pcb_layers(pcb, Path(d) / "copper-out", ["F.Cu", "B.Cu", "F.SilkS", "Edge.Cuts"], width=600)
    assert Path(im.path).stat().st_size > 5000


def test_explicit_route_edit_and_guard():
    d = _project("edit")
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    s, _ = call("pcb_route", project=d, tracks=[{"from": "R1.2", "to": "D1.2", "width": 0.25}], render=False)
    assert s["unrouted_after"] == s["unrouted_before"] - 1, s
    err = call_err("pcb_route", project=d, tracks=[{"from": "R1.1", "to": "D1.2"}], render=False)
    assert err["error"] == "NET_MISMATCH"
    call("pcb_edit", project=d, ops=[
        {"op": "set_field", "ref": "U2", "field": "MPN", "value": "ATTINY85-20SU"},
        {"op": "add_via", "at": [138, 128], "net": "GND"},
        {"op": "add_text", "text": "v1", "at": [135, 128]},
        {"op": "move", "ref": "R2", "rot": 90}])
    rd, _ = call("pcb_read", project=d, what=["footprints"], refs=["U2", "R2"])
    by = {f["ref"]: f for f in rd["footprints"]}
    assert by["U2"]["mpn"] == "ATTINY85-20SU" and by["R2"]["rot"] % 360 == 90
    s, _ = call("pcb_edit", project=d, ops=[{"op": "delete", "nets": ["LED_A"]}])
    assert "tracks" in s["changed"][0], s["changed"]
    # the open-in-KiCad guard
    lock = Path(d) / "~copper_demo.kicad_pcb.lck"
    lock.write_text("{}", encoding="utf-8")
    try:
        err = call_err("pcb_place", project=d, moves=[{"ref": "R1", "x": 110, "y": 110}], render=False)
        assert err["error"] == "FILE_OPEN_IN_KICAD"
    finally:
        lock.unlink()
    # unknown ref → clean error, board untouched
    rev = projmod.revision(Path(d) / "copper_demo.kicad_pcb")
    err = call_err("pcb_place", project=d, moves=[{"ref": "R99", "x": 1, "y": 1}], render=False)
    assert err["error"] == "NOT_FOUND"
    assert projmod.revision(Path(d) / "copper_demo.kicad_pcb") == rev


USB_PARTS = [
    ("J1", "Connector:USB_C_Receptacle_USB2.0_16P", "USB-C", "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal",
     "C2765186", (40, 60), {"A4": "VBUS", "A1": "GND", "SH": "GND", "A5": "CC1", "B5": "CC2", "A6": "DP", "A7": "DM"}),
    ("R3", "Device:R", "5.1k", "Resistor_SMD:R_0603_1608Metric", "C23186", (90, 120), {"1": "CC1", "2": "GND"}),
    ("R4", "Device:R", "5.1k", "Resistor_SMD:R_0603_1608Metric", "C23186", (110, 120), {"1": "CC2", "2": "GND"}),
    ("U1", "Regulator_Linear:AMS1117-3.3", "AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", "C6186",
     (70, 40), {"1": "GND", "2": "+3V3", "3": "VBUS"}),
    ("C1", "Device:C", "10uF", "Capacitor_SMD:C_0805_2012Metric", "C15850", (50, 90), {"1": "VBUS", "2": "GND"}),
    ("C2", "Device:C", "10uF", "Capacitor_SMD:C_0805_2012Metric", "C15850", (90, 90), {"1": "+3V3", "2": "GND"}),
    ("D1", "Device:LED", "RED", "LED_SMD:LED_0603_1608Metric", "C2286", (220, 40), {"2": "LED_A", "1": "GND"}),
    ("R1", "Device:R", "1k", "Resistor_SMD:R_0603_1608Metric", "C21190", (190, 40), {"1": "+3V3", "2": "LED_A"}),
]


def test_usb_connector_mouth_on_edge():
    d = _TMP / "usb"
    if d.exists():
        shutil.rmtree(d)
    build(d, parts=USB_PARTS)
    d = str(d)
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=30, height=25, render=False)
    s, _ = call("pcb_place", project=d, auto=True, render=False)
    assert s["verify"]["courtyard_overlaps"] == 0
    snap = board.read(projmod.resolve(d), what=["geom"])
    j1 = next(f for f in snap["footprints"] if f["ref"] == "J1")
    assert "edge_hint" in j1
    # the footprint's "PCB Edge" marker must sit on a board edge
    hx, hy = place.tf(j1["edge_hint"][0], j1["edge_hint"][1], j1["rot"])
    ex, ey = j1["x"] + hx, j1["y"] + hy
    x0, y0, x1, y1 = snap["outline"]["bbox"]
    assert min(abs(ex - x0), abs(ex - x1), abs(ey - y0), abs(ey - y1)) < 0.05, (ex, ey, snap["outline"]["bbox"])


def test_sync_updates_fields_and_removes():
    d = _project("resync")
    call("pcb_sync", project=d, render=False)
    sch = Path(d) / "copper_demo.kicad_sch"
    sch.write_text(sch.read_text(encoding="utf-8").replace('"C25804"', '"C99999"'), encoding="utf-8")
    s, _ = call("pcb_sync", project=d, render=False)
    assert any(u.startswith("updated R2") and "LCSC=C99999" in u for u in s["changed"]), s["changed"]
    # delete R1 from the schematic: reported as extra, removed only on request
    tree = sexpr.loads(sch.read_text(encoding="utf-8"))
    tree[:] = [e for e in tree if not (isinstance(e, list) and e and e[0] == "symbol" and
                                       any(isinstance(p, list) and p[:3] == ["property", "Reference", "R1"] for p in e))]
    sch.write_text(sexpr.dumps(tree) + "\n", encoding="utf-8")
    s, _ = call("pcb_sync", project=d, render=False)
    assert s["extra"] == ["R1"]
    s, _ = call("pcb_sync", project=d, remove_extra=True, render=False)
    assert s["removed"] == ["R1"] and s["verify"]["drc"]["parity"] == 0


# ── regressions found by adversarial testing ──────────────────────────────
def _spaced(name, parts=PARTS, proj="my board"):
    """Project in a folder AND with a file name containing spaces (common on Windows)."""
    d = _TMP / f"spaced dir {name}"
    if d.exists():
        shutil.rmtree(d)
    build(d, name=proj, parts=parts)
    return str(d)


def _sch_set_refs(sch: Path, mapping: dict):
    """Rename references in a test schematic (property + instance path), e.g. {'R1':'R2','R2':'R1'}."""
    tree = sexpr.loads(sch.read_text(encoding="utf-8"))
    for e in tree:
        if not (isinstance(e, list) and e and e[0] == "symbol"):
            continue
        cur = next((p[2] for p in e if isinstance(p, list) and p[:2] == ["property", "Reference"]), None)
        if cur not in mapping:
            continue
        for p in e:
            if isinstance(p, list) and p[:2] == ["property", "Reference"]:
                p[2] = mapping[cur]

        def walk(x):
            for y in x:
                if isinstance(y, list):
                    if y and y[0] == "reference":
                        y[1] = mapping[cur]
                    walk(y)
        walk(e)
    sch.write_text(sexpr.dumps(tree) + "\n", encoding="utf-8")


def test_sync_refuses_unannotated_references():
    parts = [("R?", "Device:R", "1k", "Resistor_SMD:R_0603_1608Metric", "C21190", (50, 40), {"1": "A", "2": "GND"}),
             ("R?", "Device:R", "2k", "Resistor_SMD:R_0603_1608Metric", "C21190", (80, 40), {"1": "A", "2": "B"})]
    d = _spaced("unannotated", parts)
    err = call_err("pcb_sync", project=d, render=False)
    assert err["error"] == "NOT_ANNOTATED", err
    assert not list(Path(d).glob("*.kicad_pcb")), "nothing may be written"


def test_sync_follows_symbol_paths_when_references_swap():
    d = _spaced("swap")
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    before = {f["ref"]: f for f in call("pcb_read", project=d, what=["footprints"])[0]["footprints"]}
    _sch_set_refs(Path(d) / "my board.kicad_sch", {"R1": "R2", "R2": "R1"})
    s, _ = call("pcb_sync", project=d, render=False)
    assert s["added"] == 0 and s["replaced"] == 0 and not s["extra"], s
    assert s["verify"]["drc"]["parity"] == 0, s["findings"]
    after = {f["ref"]: f for f in call("pcb_read", project=d, what=["footprints"])[0]["footprints"]}
    # the footprints stay where they were; only their references (and values) follow the symbols
    assert (after["R2"]["x"], after["R2"]["y"]) == (before["R1"]["x"], before["R1"]["y"])
    assert after["R2"]["value"] == "1k" and after["R1"]["value"] == "10k"


def test_sync_footprint_swap_keeps_reference_text_position():
    d = _spaced("fpswap")
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    pcb = Path(d) / "my board.kicad_pcb"

    def ref_at(ref):
        tree = sexpr.loads(pcb.read_text(encoding="utf-8"))
        for fp in sexpr.find_all(tree, "footprint"):
            for p in sexpr.find_all(fp, "property"):
                if p[1] == "Reference" and p[2] == ref:
                    return [float(v) for v in sexpr.find(p, "at")[1:3]], sexpr.value(fp, "at")
    at0, _ = ref_at("R2")
    sch = Path(d) / "my board.kicad_sch"
    sch.write_text(sch.read_text(encoding="utf-8").replace(
        '"Resistor_SMD:R_0603_1608Metric"', '"Resistor_SMD:R_0805_2012Metric"'), encoding="utf-8")
    s, _ = call("pcb_sync", project=d, render=False)
    assert any(c.startswith("replaced R2") for c in s["changed"]), s["changed"]
    at1, _ = ref_at("R2")
    assert max(abs(a - b) for a, b in zip(at0, at1)) < 1e-3, (at0, at1)
    assert s["verify"]["drc"]["parity"] == 0


def test_mounting_holes_never_reuse_schematic_references():
    parts = [("H1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "C21190", (50, 40), {"1": "+3V3", "2": "GND"}),
             ("H2", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "C21190", (80, 40), {"1": "+3V3", "2": "GND"})]
    d = _spaced("holes", parts)
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=30, height=30, holes="M3", render=False)
    call("pcb_setup", project=d, holes="M2.5", render=False)   # re-run replaces only the board-only holes
    refs = [f["ref"] for f in call("pcb_read", project=d, what=["footprints"])[0]["footprints"]]
    assert sorted(refs) == ["H1", "H2", "H3", "H4", "H5", "H6"], refs
    s, _ = call("pcb_sync", project=d, render=False)
    assert s["verify"]["drc"]["parity"] == 0 and not s["extra"] and s["changed"] == [], s


def test_place_around_rotated_locked_and_bottom_parts():
    d = _spaced("rotlock")
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=45, height=35, holes="M3", render=False)
    call("pcb_place", project=d, render=False, moves=[{"ref": "C1", "x": 110, "y": 110, "rot": 45, "side": "bottom"},
                                                       {"ref": "U2", "x": 122, "y": 117, "rot": 30, "lock": True}])
    s, _ = call("pcb_place", project=d, auto=True, render=False)
    assert s["verify"]["drc"]["errors"] == 0, [f for f in s["findings"] if f["severity"] == "error"]
    by = {f["ref"]: f for f in call("pcb_read", project=d, what=["footprints"])[0]["footprints"]}
    assert (by["U2"]["x"], by["U2"]["y"], by["U2"]["rot"]) == (122, 117, 30) and by["U2"].get("locked")
    assert by["C1"]["side"] == "bottom" and (by["C1"]["x"], by["C1"]["y"]) == (110, 110)
    # the placer's box for a rotated / bottom part must cover KiCad's real courtyard box
    snap = board.read(projmod.resolve(d), what=["geom"])
    for f in snap["footprints"]:
        if f["ref"] in ("C1", "U2"):
            r, k = place.Part(f).rect(), f["bbox"]
            assert r[0] <= k[0] + 0.05 and r[1] <= k[1] + 0.05 and r[2] >= k[2] - 0.05 and r[3] >= k[3] - 0.05, (f["ref"], r, k)
    err = call_err("pcb_place", project=d, moves=[{"ref": "R1", "x": 110}], render=False)
    assert err["error"] == "BAD_ARGUMENT"


UNLOCK_U2 = '{"ref":"U2","unlock":true}'


def test_locked_parts_unlock_move_and_skips_are_reported():
    """A locked footprint could be neither unlocked nor moved (the lock check ran before lock=false was
    read), an explicit move of one was dropped without an error, and auto placement never said which parts
    it had left alone."""
    d = _spaced("locks")
    pcb = Path(d) / "my board.kicad_pcb"
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=45, height=35, holes="M3", render=False)
    call("pcb_place", project=d, auto=True, render=False)

    def fp(ref):
        return next(f for f in call("pcb_read", project=d, what=["footprints"], refs=[ref])[0]["footprints"])

    # a move item with only lock=true locks; nothing moves
    at0 = (fp("U2")["x"], fp("U2")["y"], fp("U2")["rot"])
    s, _ = call("pcb_place", project=d, moves=[{"ref": "U2", "lock": True}], render=False)
    assert s["lock"] == {"locked": ["U2"]} and fp("U2").get("locked"), s
    assert (fp("U2")["x"], fp("U2")["y"], fp("U2")["rot"]) == at0

    # an explicit move of a locked part: a clear error with a ready fix, nothing written
    rev = projmod.revision(pcb)
    rot = (at0[2] + 180) % 360
    for tool_, args in (("pcb_place", {"moves": [{"ref": "R1", "rot": (fp("R1")["rot"] + 180) % 360},
                                                 {"ref": "U2", "rot": rot}]}),
                        ("pcb_place", {"moves": [{"ref": "U2", "rot": rot, "lock": True}]}),
                        ("pcb_edit", {"ops": [{"op": "move", "ref": "U2", "rot": rot}]})):
        err = call_err(tool_, project=d, render=False, **args)
        assert err["error"] == "LOCKED" and "U2" in err["message"] and '"unlock": true' in err["fix"], err
        assert projmod.revision(pcb) == rev, "a refused move must not write the board"

    # auto placement reports every part it left alone, with the reason and a fix
    s, _ = call("pcb_place", project=d, auto=True, refs=["U2", "R1", "ZZ9"], render=False)
    sk = {p["ref"]: p for p in s["skipped"]}
    assert sk["U2"]["skipped"] == "locked" and UNLOCK_U2 in sk["U2"]["fix"], s["skipped"]
    assert sk["ZZ9"]["skipped"] == "not on the board" and "R1" not in sk, s["skipped"]
    f = next(f for f in s["findings"] if f["code"] == "PARTS_SKIPPED")
    assert f["severity"] == "warning" and "U2 (locked)" in f["message"] and UNLOCK_U2 in f["fix"], f
    call("pcb_place", project=d, render=False, moves=[{"ref": "C1", "side": "bottom"}])
    s, r = call("pcb_place", project=d, auto=True, keep=["J1"], render=False)
    sk = {p["ref"]: p["skipped"] for p in s["skipped"]}
    assert sk["U2"] == "locked" and sk["J1"] == "in keep=[...]" and sk["C1"].startswith("on the bottom"), sk
    assert all(sk[h].startswith("board-only") for h in ("H1", "H2", "H3", "H4")), sk
    assert set(sk) == {"U2", "J1", "C1", "H1", "H2", "H3", "H4"}, sk
    f = next(f for f in s["findings"] if f["code"] == "PARTS_SKIPPED")
    assert f["severity"] == "info" and "H1" not in f["message"] and "J1" not in f["message"], f
    assert "skipped 2 (" in r["content"][0]["text"], r["content"][0]["text"][:300]
    dry, _ = call("pcb_place", project=d, auto=True, dry_run=True, render=False)
    assert {p["ref"]: p["skipped"] for p in dry["skipped"]}["U2"] == "locked"
    assert (fp("U2")["x"], fp("U2")["y"], fp("U2")["rot"]) == at0 and fp("U2").get("locked")

    # lock=false in a move item unlocks without moving (it used to be skipped as "locked")
    s, _ = call("pcb_place", project=d, moves=[{"ref": "U2", "lock": False}], render=False)
    assert not fp("U2").get("locked") and s["lock"] == {"unlocked": ["U2"]} and not s["skipped"], s
    assert (fp("U2")["x"], fp("U2")["y"], fp("U2")["rot"]) == at0

    # pcb_edit lock / unlock ops; unlock=true on a move moves a locked part, lock=true re-locks it there
    e, _ = call("pcb_edit", project=d, ops=[{"op": "lock", "refs": ["U2", "R1"]}])
    assert e["changed"] == ["locked U2, R1"] and fp("U2").get("locked") and fp("R1").get("locked"), e
    e, _ = call("pcb_edit", project=d, ops=[{"op": "lock", "refs": ["U2"]}])
    assert "already locked" in e["changed"][0], e
    e, _ = call("pcb_edit", project=d, ops=[{"op": "move", "ref": "U2", "rot": rot, "unlock": True, "lock": True}])
    assert fp("U2")["rot"] % 360 == rot and fp("U2").get("locked"), e
    e, _ = call("pcb_edit", project=d, ops=[{"op": "move", "ref": "U2", "rot": at0[2], "unlock": True}])
    assert fp("U2")["rot"] % 360 == at0[2] % 360 and not fp("U2").get("locked") and "(unlocked)" in e["changed"][0], e
    e, _ = call("pcb_edit", project=d, ops=[{"op": "unlock", "all": True}])
    assert e["changed"] == ["unlocked R1"] and not fp("R1").get("locked"), e
    assert fp("H1").get("locked"), "all=true leaves the board-only mounting holes locked"
    assert call_err("pcb_edit", project=d, ops=[{"op": "unlock"}])["error"] == "BAD_ARGUMENT"
    assert call_err("pcb_edit", project=d, ops=[{"op": "lock", "refs": ["ZZ9"]}])["error"] == "NOT_FOUND"
    err = call_err("pcb_place", project=d, auto=True, moves=[{"ref": "ZZ9", "unlock": True}], render=False)
    assert err["error"] == "NOT_FOUND", err

    # the fix the report gives: unlock in the same auto call - the part is free for this very placement
    call("pcb_place", project=d, render=False, moves=[{"ref": "C1", "side": "top"}, {"ref": "U2", "lock": True}])
    assert fp("U2").get("locked")
    s, _ = call("pcb_place", project=d, auto=True, render=False,
                moves=[{"ref": "U2", "unlock": True}, {"ref": "J1", "lock": True}])
    assert "U2" not in {p["ref"] for p in s["skipped"]}, s["skipped"]
    assert not any(f["code"] == "PARTS_SKIPPED" for f in s["findings"]), s["findings"]
    assert s["lock"] == {"unlocked": ["U2"], "locked": ["J1"]}, s["lock"]
    assert not fp("U2").get("locked") and fp("J1").get("locked")
    assert s["verify"]["drc"]["errors"] == 0, [f for f in s["findings"] if f["severity"] == "error"]


def test_auto_skipped_names_every_part_left_alone():
    snap = {"footprints": [{"ref": "U1", "side": "top"}, {"ref": "U2", "side": "top", "locked": True},
                           {"ref": "C1", "side": "bottom"}, {"ref": "H1", "side": "top", "locked": True, "flags": ["board_only"]},
                           {"ref": "R1", "side": "top"}, {"ref": "R2", "side": "top"}]}
    sk = {p["ref"]: p for p in place.auto_skipped(snap, ["U1"], keep=["R1"], unplaced=["R2"])}
    assert list(sk) == ["U2", "C1", "H1", "R1", "R2"]
    assert sk["U2"]["skipped"] == "locked" and sk["U2"]["fix"] == "pcb_place(auto=true, moves=[%s])" % UNLOCK_U2
    assert sk["R1"]["skipped"] == "in keep=[...]" and sk["R2"]["skipped"] == "no legal spot"
    # refs=[...]: only the parts that were asked for are in scope
    sk = place.auto_skipped(snap, ["U1"], refs=["U1", "U2", "X9", "U2"])
    assert [(p["ref"], p["skipped"]) for p in sk] == [("U2", "locked"), ("X9", "not on the board")]


def test_bottom_side_transform_matches_pcbnew():
    # pad offsets of a 2x3 header flipped to the bottom at 30° (measured with pcbnew)
    p = place.Part({"ref": "J9", "x": 0, "y": 0, "rot": 30, "side": "bottom", "lbbox": [-1, -1, 1, 1],
                    "pad_list": [{"num": "2", "lx": 2.54, "ly": 0, "w": 1, "h": 1},
                                 {"num": "3", "lx": 0, "ly": 2.54, "w": 1, "h": 1}]})
    x2, y2 = p.pad_pos(p.pads[0])
    x3, y3 = p.pad_pos(p.pads[1])
    assert abs(x2 - 2.2) < 0.01 and abs(y2 + 1.27) < 0.01
    assert abs(x3 + 1.27) < 0.01 and abs(y3 + 2.2) < 0.01


def test_layer_changes_and_pour_layers_are_validated():
    d = _spaced("layers")
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    assert call_err("pcb_pour", project=d, net="GND", layers=["In1.Cu"], render=False)["error"] == "BAD_LAYER"
    call("pcb_setup", project=d, layers=4, render=False)
    call("pcb_pour", project=d, net="+3V3", layers=["In2.Cu"], render=False)
    rev = projmod.revision(Path(d) / "my board.kicad_pcb")
    assert call_err("pcb_setup", project=d, layers=2, render=False)["error"] == "LAYERS_IN_USE"
    assert projmod.revision(Path(d) / "my board.kicad_pcb") == rev
    call("pcb_pour", project=d, net="+3V3", remove=True, render=False)
    s, _ = call("pcb_setup", project=d, layers=2, render=False)
    assert s["copper_layers"] == 2


def test_setup_refuses_everything_while_board_open():
    d = _spaced("openguard")
    call("pcb_sync", project=d, render=False)
    pro = Path(d) / "my board.kicad_pro"
    rev = projmod.revision(pro)
    lock = Path(d) / "~my board.kicad_pcb.lck"
    lock.write_text("{}", encoding="utf-8")
    try:
        assert call_err("pcb_setup", project=d, width=40, height=30, render=False)["error"] == "FILE_OPEN_IN_KICAD"
    finally:
        lock.unlink()
    assert projmod.revision(pro) == rev, "rules must not be written to the .kicad_pro while the board is open"


def test_patch_dsn_handles_wrapped_classes_and_odd_names():
    from copper_kicad.pcb import route
    dsn = _TMP / "t.dsn"
    dsn.write_text('''(pcb x
  (network
    (net GND
      (pins U1-1 U2-4)
    )
    (net "MY NET"
      (pins U1-2 R1-1)
    )
    (net GND2
      (pins U1-3 R1-2)
    )
    (class kicad_default "" N1 N2 N3 N4 N5 N6 N7 N8 N9 N10 N11 N12 N13 N14 N15 N16 N17
      GND2 "MY NET" XTAL1
      (circuit
        (use_via "Via[0-1]_600:300_um")
      )
      (rule (width 200) (clearance 200))
    )
    (class Power GND +3V3)
  )
  (wiring (wire (path F.Cu 200 0 0 1 1) (net GND) (type route)))
)
''', encoding="utf-8")
    assert route._patch_dsn(dsn, ["GND", "MY NET"]) == 2
    txt = dsn.read_text(encoding="utf-8")
    tree = sexpr.loads(txt)   # still balanced / parseable
    net = sexpr.find(tree, "network")
    names = [str(n[1]) for n in sexpr.find_all(net, "net")]
    assert names == ["GND2"], names
    classes = {str(c[1]): [str(x) for x in c[2:] if not isinstance(x, list)] for c in sexpr.find_all(net, "class")}
    assert "GND" not in classes["Power"] and "+3V3" in classes["Power"]
    assert "MY NET" not in classes["kicad_default"] and "GND2" in classes["kicad_default"] and "N17" in classes["kicad_default"]
    assert "(use_via" in txt and "(net GND) (type route)" in txt   # rules and wiring untouched


def test_resolve_net_accepts_local_label_short_names():
    from copper_kicad.tools import pcb_tools as pt
    names = {"GND", "/VBUS", "/sub/SDA", "/other/SDA"}
    assert pt._resolve_net(None, "VBUS", names) == "/VBUS"
    assert pt._resolve_net(None, "GND", names) == "GND"
    try:
        pt._resolve_net(None, "SDA", names)
        raise AssertionError("ambiguous name must fail")
    except registry.ToolError as e:
        assert e.code == "NOT_FOUND" and "ambiguous" in e.message


def test_hierarchical_multi_unit_demo_sync():
    """Stock KiCad demos with multi-instance hierarchical sheets / multi-unit parts: a fresh board from
    the schematic must be parity-clean and a second sync must change nothing."""
    share = env.kicad().share
    demos = Path(share) / "demos" if share else Path("/nonexistent")
    for name in ("complex_hierarchy", "ecc83"):
        src = demos / name
        if not src.is_dir():
            continue
        d = _TMP / f"demo {name}"
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(src, d)
        pro = sorted(d.glob("*.kicad_pro"))[0]
        for f in d.glob("*.kicad_pcb"):
            f.unlink()
        s, _ = call("pcb_sync", project=str(pro), render=False)
        assert not s["errors"] and s["verify"]["drc"]["parity"] == 0, (name, s["errors"], s["findings"][:3])
        s2, _ = call("pcb_sync", project=str(pro), render=False)
        assert s2["changed"] == [], (name, s2["changed"][:5])


def test_autoroute_keeps_best_round_when_a_later_round_fails():
    """A rip-up round deletes all tracks and saves before routing; if it then fails, the best
    earlier round must be restored (it used to leave the board with no tracks)."""
    if os.environ.get("COPPER_TEST_NO_ROUTE"):
        return
    from copper_kicad.pcb import freerouting, route, worker
    d = _spaced("bestround")
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    real_run, real_worker = freerouting.run, worker.run
    calls = {"fr": 0, "imp": 0}

    def fake_run(dsn, ses, **kw):
        calls["fr"] += 1
        if calls["fr"] >= 3:     # the rip-up round: router "crashes"
            return {"returncode": 1, "seconds": 0.1, "timed_out": False, "ses_written": False, "log_tail": ["boom"]}
        return real_run(dsn, ses, **kw)

    def fake_worker(script, req, timeout=300):
        res = real_worker(script, req, timeout=timeout)
        if req.get("by_net"):
            calls["imp"] += 1
            res["after"]["unconnected_by_net"] = {"XTAL1": 3 - calls["imp"]}   # round 2 looks best
        return res
    freerouting.run, worker.run = fake_run, fake_worker
    try:
        s, _ = call("pcb_route", project=d, finish=False, render=False)
    finally:
        freerouting.run, worker.run = real_run, real_worker
    assert calls["fr"] == 3, calls
    assert any(r.get("failed") for r in s["rounds"]) and {"kept_round": 2} in s["rounds"], s["rounds"]
    assert not any(r.get("strategy") == "guided" for r in s["rounds"]), s["rounds"]
    rd, _ = call("pcb_read", project=d, what=["summary"])
    assert rd["tracks"] > 0 and rd["unconnected"] < s["unrouted_before"], rd
    # nothing left to route → no Freerouting run, board untouched
    call("pcb_pour", project=d, net="GND", render=False)
    if call("pcb_read", project=d, what=["summary"])[0]["unconnected"] == 0:
        rev = projmod.revision(Path(d) / "my board.kicad_pcb")
        s, _ = call("pcb_route", project=d, render=False)
        assert s.get("nothing_to_route") and projmod.revision(Path(d) / "my board.kicad_pcb") == rev


# ── dogfood regressions (net classes, router, pour, sync, placement, silk) ──
def _localize(sch: Path, nets):
    """Turn the fixture's global labels for ``nets`` into local labels (KiCad names them '/NET')."""
    tree = sexpr.loads(sch.read_text(encoding="utf-8"))
    for e in tree:
        if isinstance(e, list) and e and e[0] == "global_label" and e[1] in nets:
            e[0] = Sym("label")
            e[:] = [x for x in e if not (isinstance(x, list) and x and x[0] in ("shape", "fields_autoplaced"))]
    sch.write_text(sexpr.dumps(tree) + "\n", encoding="utf-8")


def _segments(pcb: Path) -> list[tuple]:
    tree = sexpr.loads(pcb.read_text(encoding="utf-8"))
    out = []
    for s in sexpr.find_all(tree, "segment"):
        a, b = sexpr.find(s, "start"), sexpr.find(s, "end")
        k = tuple(sorted([(round(float(a[1]), 4), round(float(a[2]), 4)), (round(float(b[1]), 4), round(float(b[2]), 4))]))
        out.append((k, str(sexpr.value(s, "layer")), str(sexpr.value(s, "net")), float(sexpr.value(s, "width"))))
    return out


def test_power_patterns_match_local_label_nets():
    """A template's 'VIN*' never matched the local-label net '/VIN' (battery nets routed at signal width);
    pcb_setup(power_nets=...) alone failed with 'nothing to do' and bare names matched nothing."""
    from copper_kicad.pcb import rules
    assert rules.power_variants("VBAT*") == ["VBAT*", "*/VBAT*"] and rules.power_variants("*VCC*") == ["*VCC*"]
    assert rules.pattern_matches(["BATN", "*/BATN"], ["/BATN", "BATN_X"]) == {"BATN": 0, "*/BATN": 1}
    d = _spaced("patterns")
    sch = Path(d) / "my board.kicad_sch"
    _localize(sch, {"VIN", "LED_A"})
    pro = Path(d) / "my board.kicad_pro"
    j = json.loads(pro.read_text(encoding="utf-8"))   # like the jlc-4layer template
    j["net_settings"] = {"meta": {"version": 5}, "classes": [
        {"name": "Default", "track_width": 0.15, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "Power", "track_width": 0.5, "clearance": 0.2, "via_diameter": 0.8, "via_drill": 0.4}],
        "netclass_patterns": [{"netclass": "Power", "pattern": p} for p in ("GND", "+*", "VIN*")]}
    pro.write_text(json.dumps(j, indent=2), encoding="utf-8")
    s, _ = call("pcb_sync", project=d, render=False)
    assert any("*/VIN*" in c for c in s["changed"]), s["changed"]
    nets = {n["name"]: n["class"] for n in call("pcb_read", project=d, what=["nets"])[0]["nets"]}
    assert nets["/VIN"] == "Power" and nets["/LED_A"] == "Default", nets
    s, _ = call("pcb_setup", project=d, power_nets=["LED_A"], power_track=0.6, render=False)   # no rules=…
    assert s["rules"]["power_nets_matched"] == {"LED_A": 1}, s["rules"]
    nc = s["rules"]["netclasses"]
    assert nc["Power"]["track"] == 0.6 and nc["Default"]["track"] == 0.15 and nc["Power"]["clearance"] == 0.2, nc
    nets = {n["name"]: n["class"] for n in call("pcb_read", project=d, what=["nets"])[0]["nets"]}
    assert nets["/LED_A"] == "Power", nets
    s, _ = call("pcb_setup", project=d, power_nets=["NOPE"], render=False)
    assert any(f["code"] == "POWER_PATTERN_UNMATCHED" for f in s["findings"]), s["findings"]
    # a rules preset keeps the project's class values unless they are passed
    s, _ = call("pcb_setup", project=d, rules="jlc", render=False)
    assert s["rules"]["netclasses"]["Power"]["track"] == 0.6 and s["rules"]["netclasses"]["Default"]["track"] == 0.15


def test_dsn_rules_patch_clearance_planes_and_neck():
    from copper_kicad.pcb import route
    dsn = _TMP / "rules.dsn"
    dsn.write_text('''(pcb x
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu (type signal) (property (index 0)))
    (layer In1.Cu (type signal) (property (index 1)))
    (layer B.Cu (type signal) (property (index 2)))
    (rule (width 150) (clearance 150) (clearance 37.5 (type smd_smd)))
  )
  (network
    (net "+3V3" (pins U1-1 C1-1))
    (net SIG (pins U1-2 R1-1))
    (class kicad_default "" SIG
      (circuit (use_via "Via[0-2]_600:300_um"))
      (rule (width 150) (clearance 150))
    )
    (class Power +3V3 GND
      (circuit (use_via "Via[0-2]_800:400_um"))
      (rule (width 500) (clearance 200))
    )
  )
)
''', encoding="utf-8")
    r = route._patch_rules(dsn, clearance=0.2, neck={"+3V3": 0.2}, power_layers=["In1.Cu"])
    txt = dsn.read_text(encoding="utf-8")
    tree = sexpr.loads(txt)
    assert r["necked"] == {"+3V3": 0.2} and r["plane_layers"] == ["In1.Cu"] and r["clearance"] == 0.2
    assert "(clearance 37.5 (type smd_smd))" in txt
    st = sexpr.find(tree, "structure")
    types = {str(l[1]): str(sexpr.value(l, "type")) for l in sexpr.find_all(st, "layer")}
    assert types == {"F.Cu": "signal", "In1.Cu": "power", "B.Cu": "signal"}, types
    net = sexpr.find(tree, "network")
    cls = {str(c[1]): c for c in sexpr.find_all(net, "class")}
    assert "+3V3" not in [str(x) for x in cls["Power"] if not isinstance(x, list)]
    neck = cls["copper_neck_200"]
    assert "+3V3" in [str(x) for x in neck if not isinstance(x, list)]
    assert str(sexpr.value(sexpr.find(neck, "rule"), "width")) == "200" and sexpr.find(neck, "circuit")
    # KiCad checks a pair against the larger class clearance; Freerouting uses the board default for
    # mixed pairs and a class's own value only inside the class. So: board default = largest, every
    # class keeps its own (two signal tracks may still pass at 0.15 — raising every class to 0.2 made
    # 0.4 mm-pitch QFN pads nearly impossible to leave), and the neck class keeps its source class's.
    assert str(sexpr.value(sexpr.find(st, "rule"), "clearance")) == "200"
    assert str(sexpr.value(sexpr.find(cls["kicad_default"], "rule"), "clearance")) == "150"
    assert str(sexpr.value(sexpr.find(cls["Power"], "rule"), "clearance")) == "200"
    assert str(sexpr.value(sexpr.find(neck, "rule"), "clearance")) == "200"
    assert route._signal_layers({"copper_layers": ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"], "planes": {}}, None) == \
        (["In1.Cu", "In2.Cu"], ["F.Cu", "B.Cu"])
    assert route._signal_layers({"copper_layers": ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"], "planes": {}},
                                ["F.Cu", "In2.Cu", "B.Cu"]) == (["In1.Cu"], ["F.Cu", "B.Cu", "In2.Cu"])


def test_freerouting_watchdog_kills_a_crashed_router():
    """Freerouting's routing thread can die (StackOverflowError + HeadlessException) while the JVM idles
    until the whole timeout; the watchdog must stop it within seconds and say why."""
    import subprocess as sp
    from copper_kicad.pcb import freerouting
    real = (freerouting.ensure_java, freerouting.ensure_jar, freerouting.subprocess.Popen)
    script = ("import time,sys;print('pass #1 done',flush=True);"
              "print('Exception: java.lang.StackOverflowError at PolylineTrace.combine',flush=True);"
              "print('java.awt.HeadlessException thrown from the UncaughtExceptionHandler',flush=True);time.sleep(120)")
    freerouting.ensure_java = lambda progress=None: Path(sys.executable)
    freerouting.ensure_jar = lambda progress=None: Path("x.jar")
    freerouting.subprocess.Popen = lambda cmd, **kw: real[2]([sys.executable, "-c", script], **kw)
    try:
        t = __import__("time").time()
        r = freerouting.run(_TMP / "w.dsn", _TMP / "w.ses", timeout=100)
        took = __import__("time").time() - t
    finally:
        freerouting.ensure_java, freerouting.ensure_jar, freerouting.subprocess.Popen = real
    assert took < 40 and r["crashed"] and "StackOverflowError" in r["crashed"] and not r["ses_written"], (took, r)
    assert sp  # noqa


def test_fix_hints_name_uuids_never_whole_nets():
    from copper_kicad.tools import pcb_tools as pt
    v = {"items": ["Track [GND] on B.Cu, length 3 mm", "Pad 1 [+12V] of C1 on F.Cu"], "uuids": ["t-1", "p-1"]}
    fix = pt._fix_for("shorting_items", v)
    assert "'t-1'" in fix and "p-1" not in fix and "nets" not in fix, fix
    assert "pcb_place" in pt._fix_for("clearance", {"items": ["Pad 1 [A] of R1", "Pad 2 [B] of R2"], "uuids": ["a", "b"]})


def _rf_board(d: Path):
    """A board with the EasyEDA ESP32-C3-MINI-1 (parts_import C2838502) and the stock KiCad
    RF_Module:ESP32-C3-WROOM-02 footprints, written with pcbnew (needs KiCad's Python)."""
    import base64
    import gzip
    import pcbnew as P
    d.mkdir(parents=True, exist_ok=True)
    (d / "rf.kicad_pro").write_text(json.dumps({"meta": {"filename": "rf.kicad_pro", "version": 3}}), encoding="utf-8")
    lib = d / "libs" / "copper-lcsc.pretty"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "WIFIM-SMD_ESP32-C3-MINI-1.kicad_mod").write_bytes(gzip.decompress(base64.b64decode(ESP32_C3_MINI_1)))
    b = P.CreateEmptyBoard()
    gnd = P.NETINFO_ITEM(b, "GND")
    b.Add(gnd)
    parts = [("U1", str(lib), "WIFIM-SMD_ESP32-C3-MINI-1"),
             ("U2", env.find_lib("fp", "RF_Module", d).path, "ESP32-C3-WROOM-02"),
             ("C1", env.find_lib("fp", "Capacitor_SMD", d).path, "C_0603_1608Metric")]
    for i, (ref, path, name) in enumerate(parts):
        fp = P.FootprintLoad(str(path), name)
        fp.SetFPID(P.LIB_ID(Path(path).stem, name))
        fp.SetReference(ref)
        fp.SetPosition(P.VECTOR2I(P.FromMM(200 + 30 * i), P.FromMM(100)))
        for pad in fp.Pads():
            if pad.GetNumber() in ("1", "2") and ref == "C1" or pad.GetNumber() == "1":
                pad.SetNet(gnd)
        b.Add(fp)
    P.SaveBoard(str(d / "rf.kicad_pcb"), b)
    return str(d)


def test_rf_modules_antenna_on_edge_with_keepouts():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        return
    d = _rf_board(_TMP / "rf modules")
    snap = board.read(projmod.resolve(d), what=["geom"])
    ants = {f["ref"]: f.get("antenna") for f in snap["footprints"]}
    assert ants["U1"] and ants["U2"] and not ants["C1"], ants      # EasyEDA + stock RF_Module detected
    call("pcb_setup", project=d, width=55, height=45, render=False)
    s, _ = call("pcb_place", project=d, auto=True, render=False)
    assert s["verify"]["courtyard_overlaps"] == 0, s
    after = board.read(projmod.resolve(d), what=["geom"])
    bx0, by0, bx1, by1 = after["outline"]["bbox"]
    for f in after["footprints"]:
        if f["ref"] not in ("U1", "U2"):
            continue
        p = place.Part(f)
        poly = place.antenna_polygon(p)
        xs, ys = [q[0] for q in poly], [q[1] for q in poly]
        gaps = {"left": min(xs) - bx0, "right": bx1 - max(xs), "top": min(ys) - by0, "bottom": by1 - max(ys)}
        edge = min(gaps, key=gaps.get)
        assert gaps[edge] < 1.6, (f["ref"], gaps)                     # antenna flush with a board edge …
        acx, acy = sum(xs) / 4, sum(ys) / 4
        out = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[edge]
        assert (acx - p.x) * out[0] + (acy - p.y) * out[1] > 0, (f["ref"], edge)   # … pointing out of the board
    txt = (Path(d) / "rf.kicad_pcb").read_text(encoding="utf-8")
    for ref in ("U1", "U2"):
        k = txt.index(f'"copper-antenna-{ref}"')
        zone = txt[txt.rfind("(zone", 0, k):k + 600]
        assert "(tracks not_allowed)" in zone and "(vias not_allowed)" in zone and "(copperpour not_allowed)" in zone
    s, _ = call("pcb_pour", project=d, net="GND", render=False)
    assert any(c.startswith("filled 2 of 2 copper zones") for c in s["changed"]), s["changed"]


def test_placer_edge_fallback_mcu_fallback_and_roles():
    ol = {"bbox": [0, 0, 40, 20], "polygon": [[0, 0], [40, 0], [40, 20], [0, 20]]}

    def fp(ref, value, fpid, lbbox, pads, x=60, y=60):
        return {"ref": ref, "value": value, "footprint": fpid, "x": x, "y": y, "rot": 0, "side": "top",
                "lbbox": lbbox, "pad_list": pads}
    hdr = [{"num": str(k + 1), "lx": 0, "ly": k * 2.54, "w": 1.7, "h": 1.7, "net": f"S{k}", "type": "tht"} for k in range(12)]
    snap = {"outline": ol, "footprints": [
        fp("J1", "PWR", "Connector_JST:JST_PH_B2B", [-3, -2, 3, 3],
           [{"num": "1", "lx": -1, "ly": 0, "net": "VIN", "type": "tht"}, {"num": "2", "lx": 1, "ly": 0, "net": "GND", "type": "tht"}]),
        fp("J2", "HDR", "Connector_PinHeader_2.54mm:PinHeader_1x12", [-1.3, -1.3, 1.3, 29.3], hdr)]}
    r = place.auto_place(snap)
    j2 = next(m for m in r["moves"] if m["ref"] == "J2")
    part = place.Part(dict(snap["footprints"][1], x=j2["x"], y=j2["y"], rot=j2["rot"]))
    x0, y0, x1, y1 = part.rect()
    assert min(y0 - 0.5, 19.5 - y1) < 1.0 and not r["unplaced"], (j2, part.rect())   # long header on a long edge
    # MCU: the centre cross is blocked → the full search still finds a corner
    mcu_pads = [{"num": str(i + 1), "lx": -3 + (i % 8) * 0.8, "ly": -3 if i < 8 else 3, "net": f"N{i}"} for i in range(16)]
    snap = {"outline": {"bbox": [0, 0, 30, 30]}, "obstacles": [{"bbox": [0, 11, 30, 19]}, {"bbox": [11, 0, 19, 30]}],
            "footprints": [fp("U1", "STM32", "Package_QFP:LQFP-16", [-3.5, -3.5, 3.5, 3.5], mcu_pads)]}
    r = place.auto_place(snap)
    assert not r["unplaced"] and r["moves"] and r["roles"]["mcu"] == ["U1"], r
    # roles: crystal at the oscillator pin (not a shared GND/TESTEN pin), every IC gets a decap (the one drawn
    # next to it in the schematic), ESD array at its connector, a new cap finds a kept (fixed) IC
    mcu = fp("U1", "RP2040", "Package_DFN_QFN:QFN-56", [-4, -4, 4, 4],
             [{"num": "19", "lx": -3, "ly": 0, "net": "GND", "fn": "TESTEN"},
              {"num": "20", "lx": -3, "ly": 1, "net": "/XIN", "fn": "XIN"},
              {"num": "21", "lx": -3, "ly": 2, "net": "/XOUT", "fn": "XOUT"},
              {"num": "1", "lx": 3, "ly": 0, "net": "+3V3"}, {"num": "10", "lx": 3, "ly": 1, "net": "+3V3"},
              {"num": "30", "lx": 0, "ly": -3, "net": "/DP"}, {"num": "31", "lx": 0, "ly": 3, "net": "/DM"}])
    mcu["sch"] = ["root|/", 100, 100]
    flash = fp("U2", "W25Q128", "Package_SO:SOIC-8", [-3, -3, 3, 3],
               [{"num": "8", "lx": 2, "ly": -1, "net": "+3V3"}, {"num": "4", "lx": -2, "ly": 1, "net": "GND"},
                {"num": "1", "lx": -2, "ly": -1, "net": "/CS"}])
    flash["sch"] = ["root|/", 200, 100]
    xtal = fp("Y1", "12MHz", "Crystal:Crystal_SMD_3225-4Pin", [-2, -1.5, 2, 1.5],
              [{"num": "1", "lx": -1, "ly": 1, "net": "/XIN"}, {"num": "2", "lx": 1, "ly": 1, "net": "GND"},
               {"num": "3", "lx": 1, "ly": -1, "net": "/XOUT"}, {"num": "4", "lx": -1, "ly": -1, "net": "GND"}])
    caps = []
    for i, sx in enumerate((98, 104, 203)):
        c = fp(f"C{i + 1}", "100nF", "Capacitor_SMD:C_0402", [-1, -0.5, 1, 0.5],
               [{"num": "1", "lx": -0.5, "ly": 0, "net": "+3V3"}, {"num": "2", "lx": 0.5, "ly": 0, "net": "GND"}])
        c["sch"] = ["root|/", sx, 110]
        caps.append(c)
    usb = fp("J1", "USB_C", "Connector_USB:USB_C", [-4, -3, 4, 3],
             [{"num": "A6", "lx": -1, "ly": 0, "net": "/DP"}, {"num": "A7", "lx": 1, "ly": 0, "net": "/DM"},
              {"num": "A1", "lx": 3, "ly": 0, "net": "GND"}])
    esd = fp("U4", "USBLC6-2SC6", "Package_TO_SOT_SMD:SOT-23-6", [-1.5, -1.5, 1.5, 1.5],
             [{"num": "1", "lx": -1, "ly": -1, "net": "/DP"}, {"num": "3", "lx": -1, "ly": 1, "net": "/DM"},
              {"num": "2", "lx": 1, "ly": 0, "net": "GND"}])
    parts = [place.Part(x) for x in (mcu, flash, xtal, usb, esd, *caps)]
    place.classify(parts)
    by = {p.ref: p for p in parts}
    assert by["Y1"].anchor[1]["num"] in ("20", "21"), by["Y1"].anchor[1]
    assert by["C3"].role == "decap" and by["C3"].anchor[0].ref == "U2", (by["C3"].role, by["C3"].anchor)
    assert by["C1"].anchor[0].ref == "U1" and by["C2"].anchor[0].ref == "U1"
    assert by["C1"].anchor[1]["num"] != by["C2"].anchor[1]["num"]      # two caps → two different power pins
    assert by["U4"].role == "esd" and by["U4"].anchor[0].ref == "J1"
    # refs=[new cap] on a board whose IC stays put: the cap still hugs that IC; fill counts every part
    snap = {"outline": {"bbox": [0, 0, 20, 20]}, "footprints": [
        dict(flash, x=10, y=10, sch=None), dict(caps[2], x=50, y=50, sch=None),
        fp("U9", "BIG", "Package_QFP:LQFP-100", [-6, -6, 6, 6], [], x=4, y=4)]}
    snap["footprints"][2]["locked"] = True
    r = place.auto_place(snap, refs=["C3"])
    assert r["roles"]["decap"] == ["C3→U2.8"] and r["fill_ratio"] > 0.3, r
    m = r["moves"][0]
    assert abs(m["x"] - 12) + abs(m["y"] - 9) < 5, m


def test_route_existing_board_adds_only_missing_wiring():
    """pcb_route on a routed board used to duplicate every existing segment (KiCad's SES import deletes and
    re-adds unlocked copper) and could re-size existing vias; clear_existing was ignored when nothing was
    unrouted. Existing copper is now exported as fixed and kept byte-for-byte."""
    if os.environ.get("COPPER_TEST_NO_ROUTE"):
        return
    d = _spaced("existing")
    pcb = Path(d) / "my board.kicad_pcb"
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    call("pcb_route", project=d, ignore_nets=["GND"], render=False)
    call("pcb_pour", project=d, net="GND", stitch=True, render=False)
    w = call("pcb_read", project=d, what=["widths"])[0]["widths"]
    assert w and all(isinstance(v["widths"], dict) for v in w.values()) and "track_list" not in \
        call("pcb_read", project=d, what=["widths"])[0]
    call("pcb_edit", project=d, ops=[{"op": "delete", "nets": ["LED_A"]}])
    before = _segments(pcb)
    s, _ = call("pcb_route", project=d, render=False)
    after = _segments(pcb)
    assert len(after) == len(set(after)), "duplicate segments"
    assert set(before) <= set(after), "existing tracks were changed"
    new = set(after) - set(before)
    assert new and "LED_A" in {n[2] for n in new}, {n[2] for n in new}
    assert s["unrouted_after"] == 0 and s["verify"]["drc"]["errors"] == 0, s
    s, _ = call("pcb_route", project=d, clear_existing=True, ignore_nets=["GND"], render=False)
    assert not s.get("nothing_to_route") and s["rounds"], s


def test_four_layer_planes_reserved_and_fanned_out():
    if os.environ.get("COPPER_TEST_NO_ROUTE"):
        return
    d = _spaced("four layer")
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, layers=4, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    call("pcb_pour", project=d, net="GND", layers=["In1.Cu"], render=False)
    call("pcb_pour", project=d, net="+3V3", layers=["In2.Cu"], render=False)
    s, _ = call("pcb_route", project=d, render=False)
    assert s["plane_layers"] == ["In1.Cu", "In2.Cu"] and s["plane_fanout"]["fanout_vias"] > 0, s
    layers = {seg[1] for seg in _segments(Path(d) / "my board.kicad_pcb")}
    assert layers <= {"F.Cu", "B.Cu"}, layers
    # every plane-net pad reached its plane; DRC clean (a signal may be left on this tiny 2-signal-layer board)
    assert not set(s.get("unrouted_nets") or {}) & {"GND", "+3V3"}, s.get("unrouted_nets")
    assert s["verify"]["drc"]["errors"] == 0 and s["unrouted_after"] <= 2, s


def test_explicit_route_and_moves_refuse_shorts():
    d = _spaced("shorts")
    pcb = Path(d) / "my board.kicad_pcb"
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    rd = call("pcb_read", project=d, what=["pads"], refs=["D1", "R1"])[0]
    pads = {(f["ref"], p["num"]): p for f in rd["footprints"] for p in f["pad_list"]}
    px, py = pads[("D1", "2")]["x"], pads[("D1", "2")]["y"]
    h = 0.75   # a +3V3 ring around D1.2 (clear of D1.1): every path into the pad crosses it
    ring = [[px - h, py - h], [px + h, py - h], [px + h, py + h], [px - h, py + h], [px - h, py - h]]
    call("pcb_edit", project=d, ops=[{"op": "add_track", "points": ring, "net": "+3V3", "width": 0.15, "layer": "F.Cu"}])
    rev = projmod.revision(pcb)
    err = call_err("pcb_route", project=d, tracks=[{"from": "R1.2", "to": "D1.2", "layer": "F.Cu"}], render=False)
    assert err["error"] == "ROUTE_BLOCKED" and "+3V3" in err["message"], err
    assert projmod.revision(pcb) == rev
    err = call_err("pcb_place", project=d, moves=[{"ref": "R2", "x": px, "y": py}], render=False)
    assert err["error"] == "PAD_SHORT" and projmod.revision(pcb) == rev, err
    err = call_err("pcb_place", project=d, auto=True, render=False)
    assert err["error"] == "TRACKS_EXIST" and projmod.revision(pcb) == rev, err


ISLAND_PARTS = [
    ("C1", "Device:C", "1uF", "Capacitor_SMD:C_0805_2012Metric", "C28323", (50, 40), {"1": "NA", "2": "GND"}),
    ("C2", "Device:C", "1uF", "Capacitor_SMD:C_0805_2012Metric", "C28323", (80, 40), {"1": "NA", "2": "GND"}),
    ("R1", "Device:R", "1k", "Resistor_SMD:R_0603_1608Metric", "C21190", (110, 40), {"1": "NA", "2": "NB"}),
]


def test_pour_joins_islands_holding_pads():
    """A GND pad boxed in by another net's track leaves its F.Cu pour fragment isolated; the grid of stitching
    vias missed such fragments and the IC ground pin floated. pcb_pour now drops a via into every fragment
    that holds a pad of the net."""
    d = _spaced("islands", ISLAND_PARTS)
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=30, height=20, render=False)
    call("pcb_place", project=d, render=False, moves=[{"ref": "C1", "x": 108, "y": 110}, {"ref": "C2", "x": 122, "y": 110},
                                                      {"ref": "R1", "x": 115, "y": 116}])
    h = 2.6
    box = [[108 - h, 110 - h], [108 + h, 110 - h], [108 + h, 110 + h], [108 - h, 110 + h], [108 - h, 110 - h]]
    call("pcb_edit", project=d, ops=[{"op": "add_track", "points": box, "net": "NB", "width": 0.2, "layer": "F.Cu"}])
    s, _ = call("pcb_pour", project=d, net="GND", render=False)
    assert any("joining isolated GND fragments" in c and "C1.2" in c for c in s["changed"]), s["changed"]
    drc = kicad_drc(Path(d) / "my board.kicad_pcb")
    assert drc["unconnected"] <= 3, drc    # only NA / NB (boxed in / unrouted on purpose) may be left
    d2 = board.drc(projmod.resolve(d))
    assert not any("[GND]" in " ".join(u["items"]) for u in d2.get("unconnected_top", [])), d2.get("unconnected_top")


def test_sync_moves_copper_of_renamed_nets_and_drops_orphans():
    d = _spaced("renamed")
    pcb = Path(d) / "my board.kicad_pcb"
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    call("pcb_route", project=d, tracks=[{"from": "R1.2", "to": "D1.2", "width": 0.25}], render=False)
    sch = Path(d) / "my board.kicad_sch"
    sch.write_text(sch.read_text(encoding="utf-8").replace('"LED_A"', '"LED_X"'), encoding="utf-8")
    s, _ = call("pcb_sync", project=d, render=False)
    assert any("LED_A → LED_X" in c for c in s["changed"]), s["changed"]
    assert {seg[2] for seg in _segments(pcb)} == {"LED_X"} and s["verify"]["drc"]["errors"] == 0, s["verify"]
    # delete R1 and D1: LED_X vanishes, its track is orphaned (reported with uuids), remove_extra deletes it
    tree = sexpr.loads(sch.read_text(encoding="utf-8"))
    tree[:] = [e for e in tree if not (isinstance(e, list) and e and e[0] == "symbol" and any(
        isinstance(p, list) and p[:2] == ["property", "Reference"] and p[2] in ("R1", "D1") for p in e))]
    tree[:] = [e for e in tree if not (isinstance(e, list) and e and e[0] == "global_label" and e[1] == "LED_X")]
    sch.write_text(sexpr.dumps(tree) + "\n", encoding="utf-8")
    s, _ = call("pcb_sync", project=d, render=False)
    f = next(x for x in s["findings"] if x["code"] == "ORPHAN_COPPER")
    assert "uuids" in f["fix"] and _segments(pcb), f
    s, _ = call("pcb_sync", project=d, remove_extra=True, render=False)
    assert sorted(s["removed"]) == ["D1", "R1"] and not _segments(pcb), (s["changed"], _segments(pcb))


ESP32_C3_MINI_1 = (
    "H4sIAAAAAAACCu2a3W7bNhSAr9OnIIRd2F0li5Kon961jgOkbdqgwTrsKqApOhYiS4JIt3W7Fh22R9gTdi8yUj+OHIlFs1HZzS4ciYfh+c4PDylTnqzy"
    "nBdlknFg/Hx6cnpmXpwdXy4uzl3HnLvm2enLUxMaD44mb2nJkjwDju140HGiqZBd0YyWmOclMEheFLS8vE4Ijo2Dvst2pAEtu+pK8Y6KISfWfFu1Y8qI"
    "aN+Cmi89MJk7oRsi25kCYdYj6Fv+e+hazmYDvn75E5ie/fWP3z//GCJxAWYUxk83wkALyauL7M0T4L757Fr+GxB6onGacXoljKIx4HRTSPu2JQWMZiwv"
    "//ryW6cfi9ssw0L4dJuk3Ewy8A5zso7zK/AqM5c5LmMAfgUX+bYk9DF49mK+OH4yW2C2E1fwarVKSIJT8CJZlrjcgcma84I9ns1SQmNskWwGHoFWRsWo"
    "SppvppVjJM9E1KQdaU5wmu7AcgfmVYjBSoT7eTLHsaA3Ax8TN6DUsbFLyMrzIz90lvGSksiNwoigAFVh5viKAaONKBgI94v5xVx6cj5/Wo0oylwQ+Q4Y"
    "r+mKljQj1BD3i5OHD8WUOJpgDmxgQseyfWBPpWSf2oskvb4wKhldrSjhTNweTVZ5xuXN0YQlHyiAAE7rJl8n5DqjjAHbgqgSyj/ic2jIG5xupRHfmquN"
    "YWKyoNtmneDlCEYdY47ZmlIuDLvh20r4Ookp2FE2giWylJKCi3oz/q+oMSvqP0gz5rwEbBPL+1VxSZKSpFTqJlTEtgSmSKYoR1jzslgKENpLGC/za1oZ"
    "8C6J+VowbL9iTPiuEEnL0yRucAKQpOne+Nu+KS0ILBhFkZhGNzYEbWuA730bn+Vd+vG7K2b9xGjZsaDI0538l6KJ7PuddDqQPlt1UPciZAV7wVC7N8Tv"
    "i2QwbSvqjBuSIAsdDuoJ9mMUgblTVr4/LgLc9bqVoEN/BgTB/dvqi6j5vXR0DOu3mwGqyQbvMtluT/U0yWitF5e8S6smeTtZbNtW1pqD1LO9v20qsWaX"
    "ax5IxoH2VpVue0Rk38/7ZXbbupADC+bBWulbYWf5vCN1eMW+bUIpdqEDr8Nqh7CcruehXLNCpNw20N1KaV7yX+KODZy+52DLZOcPH8Xj5OL14uV88el7"
    "9tRvbKO2FcrP0Fbq9J6YxGZvQEPupqCNiCCLUmoSAJygNqFR7bWqa5tY8/VFXs4x47S6O8Ps2ugSHDXBrXdKLRhXjXEsTa54aga0kDZXkBoj1InlVQ/G"
    "V2JsPYBADdDoRqik6MxJpKRom13QVjJ0FgpUl7wuRwZK3qsz7jWZCXVgBkre00sYKPgmFVoxAwUvZ5VWxkC1N/WhFTNQ8/YIqe8Xva0X0K/3Edxw+hU/"
    "Qk6cfsHrnl1Ov+JHqBOnX/Ca3ejX+wjrltMv9/36G+nQ76v0NznRAglUEDm3tBBCFaGpES2QSAVpyl0HxLWVED36oUq/qdMLR0nRmBDXVVJ0TSzXUyI0"
    "FoirrHJTkxu+arEy69VKAyIYWHD1aQ9Vm4Y+RDS49WnT7yk3cH0IqHoS0YdwBp6m9Gl3lY+E+hie8ulWH0PxlK4P4Cu/behjBEPfmfSpD5Xf/PQxmpom"
    "W8bzzU22owDB9tqlIPn5XsrRJK9eHNaHaySluMQZoSDfcnlUWh/5Cck6L0F9kLk/9SvKZJPw5C2tx16V7aH+zbF+c45vW4GD5AU6XnNU13bAuiM47NgP"
    "GJRXf+G0B+jKmwPB7uuKw8PS3sGgFw0sbVHgReB2iKHloerPP07lwcIzqvp2ntwLxB4tRmadipH0N3m+F8h4MdKiGQ09ukdIrmYR6q4ygfzcXT0cVq9J"
    "++DRuz71rkL9vw/OJo9pKl+MPD89f/3s7NXxp1maLNms/oWVmRJGLDdma1xQNlP+DMYSqovqhUq+WjHKm/ekH+T5thXA5ocxtaEEp3TfD9ufI1R9Zc4x"
    "p53B9n7g9MH0wd+VQXntPiYAAA=="
)


# ── second dogfood round: DRC bookkeeping, shared router install, read/setup wording, dense 4-layer boards ──
def test_drc_refills_zones_and_records_the_result():
    """board.drc() used to check the fills saved in the file (pcb_place / pcb_edit disagreed with verify)
    and never told verify / project_summary about its result."""
    from copper_kicad.review import verify
    d = _project("drcrec")
    call("pcb_sync", project=d, render=False)
    pr = projmod.resolve(d)
    seen = []
    real = env.run

    def spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)
    env.run = spy
    try:
        out = board.drc(pr, parity=True)
        board.drc(pr, parity=False, record=False)
    finally:
        env.run = real
    assert "--refill-zones" in seen[0] and "--save-board" not in seen[0], seen[0]
    st = verify.load_state(pr).get("drc") or {}
    assert st.get("source") == "pcb" and st.get("revision") == projmod.revision(pr.pcb), st
    assert st.get("unconnected") == out["unconnected"] and st.get("parity") == out["parity"], st
    # a run without the parity check must not overwrite the recorded parity count with a 0 it never measured
    verify.save_state(pr, "drc", dict(st, parity=7))
    board.drc(pr, parity=False)
    assert verify.load_state(pr)["drc"].get("parity") in (None, 7), verify.load_state(pr)["drc"]


def test_freerouting_falls_back_to_the_shared_tools_folder():
    """With COPPER_HOME pointing somewhere else (tests, sandboxes, a second profile) the router installed in
    ~/.copper/tools was ignored and ~100 MB downloaded again."""
    from copper_kicad.pcb import freerouting as fr
    shared, home = _TMP / "shared tools", _TMP / "other home"
    java = shared / f"jre-{fr.JAVA_MAJOR}" / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    java.parent.mkdir(parents=True)
    java.write_bytes(b"")
    jar = shared / f"freerouting-{fr.FREEROUTING_VERSION}.jar"
    jar.write_bytes(b"0" * 1_100_000)
    saved = {k: os.environ.pop(k, None) for k in ("COPPER_HOME", "COPPER_JAVA", "COPPER_FREEROUTING_JAR")}
    real_shared, real_which = fr._shared_tools, fr.shutil.which
    os.environ["COPPER_HOME"] = str(home)
    fr._shared_tools = lambda: shared
    fr.shutil.which = lambda name: None
    try:
        assert fr.tools_dir() == home / "tools"
        assert fr.jar_path() == jar and fr.find_java() == java, (fr.jar_path(), fr.find_java())
        assert fr.ensure_jar() == jar and fr.status()["ready"]          # no download
        own = home / "tools" / jar.name                                 # an install in COPPER_HOME wins
        own.write_bytes(b"1" * 1_100_000)
        assert fr.jar_path() == own
        own.write_bytes(b"1" * 10)                                      # a truncated download does not
        assert fr.jar_path() == jar
        jar.unlink()                                                    # nothing anywhere: the COPPER_HOME path
        assert fr.jar_path() == own
    finally:
        fr._shared_tools, fr.shutil.which = real_shared, real_which
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    desc = registry.TOOLS["pcb_route"].description
    assert "$COPPER_HOME/tools (default ~/.copper/tools" in desc and "into ~/.copper/tools" not in desc


_TEARDROP = """	(zone
		(net "GND")
		(layer "F.Cu")
		(uuid "%s")
		(name "$teardrop_padvia$")
		(hatch full 0.1)
		(priority 30001)
		(attr
			(teardrop
				(type padvia)
			)
		)
		(connect_pads yes
			(clearance 0)
		)
		(min_thickness 0.0254)
		(filled_areas_thickness no)
		(fill yes
			(thermal_gap 0.5)
			(thermal_bridge_width 0.5)
			(island_removal_mode 1)
			(island_area_min 10)
		)
		(polygon
			(pts
				(xy %s 110) (xy %s 110) (xy %s 110.4)
			)
		)
	)
"""


def test_pcb_read_counts_teardrops_and_setup_lists_what_it_counts():
    import copper_kicad.tools.pcb_tools as pt
    d = _project("teardrops")
    pcb = Path(d) / "copper_demo.kicad_pcb"
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    call("pcb_pour", project=d, net="GND", render=False)
    txt = pcb.read_text(encoding="utf-8").rstrip()
    assert txt.endswith(")")
    tears = "".join(_TEARDROP % (_u(), 110 + i, 110.5 + i, 110.2 + i) for i in range(3))
    pcb.write_text(txt[:-1] + tears + ")\n", encoding="utf-8")
    rd, _ = call("pcb_read", project=d, what=["zones"])
    # one count for the teardrops (a board with teardrops has one per pad/via), real pours listed as before
    assert rd["teardrop_zones"] == 3, rd.get("teardrop_zones")
    assert rd["zones"] and all(not (z["name"] or "").startswith("$teardrop") for z in rd["zones"]), rd["zones"]
    assert {z["net"] for z in rd["zones"]} == {"GND"}
    # OUTSIDE_OUTLINE: the count and the listed refs come from one list ("24 footprints … <20 names>" before)
    snap = {"outline": {"bbox": [100, 100, 140, 130]},
            "footprints": [{"ref": f"R{i}", "x": 10.0, "y": 10.0} for i in range(45)] + [{"ref": "U1", "x": 120, "y": 110}]}
    f = [x for x in pt._findings({}, snap) if x["code"] == "OUTSIDE_OUTLINE"][0]
    assert f["message"].startswith("45 footprints") and "… and 5 more" in f["message"] and len(f["refs"]) == 45, f
    assert f["message"].count("R") == 40 and "U1" not in f["refs"]
    snap["footprints"] = snap["footprints"][:24] + snap["footprints"][-1:]
    f = [x for x in pt._findings({}, snap) if x["code"] == "OUTSIDE_OUTLINE"][0]
    assert f["message"].startswith("24 footprints") and "more" not in f["message"] and f["message"].count("R") == 24, f


def test_finished_nets_become_polygon_keepouts():
    """Round 2 freezes finished nets as keepouts. Path-shaped keepouts were not reliably honoured by
    Freerouting (new traces crossed finished ones: 'tracks_crossing' on the RP2040 board)."""
    from copper_kicad.pcb import route
    dsn = _TMP / "keep.dsn"
    dsn.write_text("""(pcb x
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu (type signal) (property (index 0)))
    (layer B.Cu (type signal) (property (index 1)))
    (boundary (path pcb 0  0 0  1000 0  1000 -1000  0 -1000  0 0))
    (via "Via[0-1]_600:300_um")
    (rule (width 150) (clearance 150))
  )
  (network
    (net DONE (pins U1-1 R1-1))
    (net OPEN (pins U1-2 R1-2))
    (class kicad_default "" DONE OPEN (circuit (use_via "Via[0-1]_600:300_um")) (rule (width 150) (clearance 150)))
  )
  (wiring
    (wire (path F.Cu 200  1000 -2000  3000 -2000  3000 -5000)(net DONE)(type fix))
    (via "Via[0-1]_600:300_um"  3000 -5000 (net DONE)(type fix))
    (wire (path B.Cu 150  0 0  500 500)(net OPEN)(type fix))
  )
)
""", encoding="utf-8")
    assert route._patch_dsn(dsn, ["DONE"]) == 1
    assert route._wiring_to_keepouts(dsn, ["DONE"]) == 2
    txt = dsn.read_text(encoding="utf-8")
    tree = sexpr.loads(txt)
    kos = sexpr.find_all(sexpr.find(tree, "structure"), "keepout")
    shapes = [k[2] for k in kos]
    polys = [s for s in shapes if str(s[0]) == "polygon"]
    assert not [s for s in shapes if str(s[0]) == "path"], shapes
    assert len(polys) == 2 and all(str(s[1]) == "F.Cu" and len(s) == 3 + 8 for s in polys), polys   # one per segment
    xs = [float(v) for v in polys[0][3::2]]
    ys = [float(v) for v in polys[0][4::2]]
    assert (min(xs), max(xs), min(ys), max(ys)) == (900.0, 3100.0, -2100.0, -1900.0), (xs, ys)       # width + square ends
    assert len([s for s in shapes if str(s[0]) == "circle"]) == 2                                    # the via, per signal layer
    wires = sexpr.find_all(sexpr.find(tree, "wiring"), "wire")
    assert len(wires) == 1 and "OPEN" in txt and "(net DONE" not in txt
    # copper-to-edge: Freerouting only keeps the ordinary clearance to the outline; a strip either side of it
    # makes up the difference to KiCad's edge rule (a via landed 0.22 mm from the edge of the RP2040 board)
    assert route._edge_keepouts(dsn, 0, ["F.Cu", "B.Cu"]) == 0
    assert route._edge_keepouts(dsn, 0.11, ["F.Cu", "B.Cu"]) == 8          # 4 outline segments x 2 layers
    tree = sexpr.loads(dsn.read_text(encoding="utf-8"))
    edge = [k[2] for k in sexpr.find_all(sexpr.find(tree, "structure"), "keepout") if str(k[1]).startswith("copper_edge")]
    assert len(edge) == 8 and {str(s[1]) for s in edge} == {"F.Cu", "B.Cu"}
    ys = sorted({float(v) for v in edge[0][4::2]})
    assert ys == [-110.0, 110.0], ys


def _pads(d, ref):
    rd, _ = call("pcb_read", project=d, what=["footprints", "pads"], refs=[ref])
    return {p["num"]: p for p in rd["footprints"][0]["pad_list"]}


def test_plane_fanout_keeps_the_escape_lanes_of_ic_pins_free():
    """RP2040 board: the fan-out via of a decoupling cap sitting beside the QFN's pad row went 'outward
    from the cap' — straight into the gap the IC's signal pins leave through. A via now avoids the
    lanes in front of other parts' signal pads when another spot is free."""
    from copper_kicad.pcb import worker
    d = _spaced("fanout lanes")
    pcb = Path(d) / "my board.kicad_pcb"
    call("pcb_sync", project=d, render=False)
    call("pcb_setup", project=d, width=40, height=30, layers=4, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    park = {"J1": (104, 104), "U1": (112, 105), "C1": (104, 110), "C2": (104, 114), "Y1": (104, 118), "C4": (104, 122),
            "C5": (108, 122), "R1": (112, 122), "D1": (116, 124), "R2": (108, 126), "J2": (134, 108), "C3": (120, 126)}
    call("pcb_place", project=d, render=False,     # only U2 and C3 matter: everything else out of the way
         moves=[{"ref": r_, "x": x, "y": y, "rot": 0, "force": True} for r_, (x, y) in park.items()]
         + [{"ref": "U2", "x": 122, "y": 114, "rot": 0, "side": "top", "force": True}])
    u2 = _pads(d, "U2")
    left = sorted((p for p in u2.values() if p["x"] < 122), key=lambda p: p["y"])
    assert len(left) == 4 and all(p["net"] not in ("GND", "+3V3") for p in left[:3]), left
    px = left[0]["x"]
    mid = (left[0]["y"] + left[1]["y"]) / 2
    # C3 (+3V3 / GND, 0603) upright beside the row, its lower pad level with the gap between pins 1 and 2
    call("pcb_place", project=d, moves=[{"ref": "C3", "x": round(px - 1.9, 3), "y": round(mid - 0.8, 3), "rot": 90,
                                         "force": True}], render=False)
    call("pcb_pour", project=d, net="GND", layers=["In1.Cu"], render=False)
    call("pcb_pour", project=d, net="+3V3", layers=["In2.Cu"], render=False)
    r = worker.run("board", {"pcb": str(pcb), "ops": [{"op": "fanout", "planes": True}], "save": True})
    fo = r["results"][0]
    assert {"C3.1", "C3.2"} <= set(fo["pads"]) and not fo["failed"], fo
    c3 = _pads(d, "C3")
    rd, _ = call("pcb_read", project=d, what=["tracks"])
    vias = []
    for pad in c3.values():
        ends = [tr["end"] for tr in rd["track_list"] if tr["type"] == "track" and
                abs(tr["start"][0] - pad["x"]) < 0.01 and abs(tr["start"][1] - pad["y"]) < 0.01]
        assert len(ends) == 1, (pad, ends)
        assert any(v["type"] == "via" and v["net"] == pad["net"] and v["at"] == ends[0] for v in rd["track_list"])
        vias.append(ends[0])

    def lane_dist(v, pin):          # distance to the straight way out of a left-row pin (≈ 2.5 mm from the pad end)
        x0, x1, y = pin["x"] - 3.4, pin["x"] - 0.9, pin["y"]
        return ((v[0] - min(max(v[0], x0), x1)) ** 2 + (v[1] - y) ** 2) ** 0.5
    worst = min(lane_dist(v, pin) for v in vias for pin in left[:3])
    assert worst >= 0.6, (worst, vias, [(p["x"], p["y"]) for p in left])
    # and the fan-out stays legal + idempotent
    assert kicad_drc(pcb)["errors"] == [] or not [e for e in kicad_drc(pcb)["errors"]
                                                  if e["type"] in ("clearance", "shorting_items", "hole_clearance")]
    again = worker.run("board", {"pcb": str(pcb), "ops": [{"op": "fanout", "planes": True}], "save": False})
    assert again["results"][0]["fanout_vias"] == 0, again["results"][0]


def test_autoroute_hands_leftovers_to_the_guided_router():
    """What Freerouting cannot finish goes to the guided router (pcb.guided.route, called with a real plan);
    its result is kept only when it routes more without adding DRC errors, otherwise Freerouting's stands."""
    if os.environ.get("COPPER_TEST_NO_ROUTE"):
        return
    from copper_kicad.pcb import route, worker
    real_worker, real_finish = worker.run, route._finish_guided
    state = {"left": 1, "guided": 0, "plans": []}

    def fake_worker(script, req, timeout=300):
        res = real_worker(script, req, timeout=timeout)
        if req.get("by_net") and res.get("after") is not None:
            res["after"]["unconnected_by_net"] = {"XTAL1": state["left"]} if state["left"] else {}
        return res

    def spy_finish(pr, sig, ignore, budget, while_open):
        state["guided"] += 1
        g = real_finish(pr, sig, ignore, budget, while_open)     # the real guided router, real plan validation
        state["plans"].append((list(sig), sorted(ignore), budget))
        state["left"] = 0                                        # ... "finished the last connection"
        return dict(g, drc=dict(g.get("drc") or {}, errors=state.get("errors", (g.get("drc") or {}).get("errors", 0))))

    def run(name, **kw):
        d = _spaced(name)
        call("pcb_sync", project=d, render=False)
        call("pcb_setup", project=d, width=40, height=30, render=False)
        call("pcb_place", project=d, auto=True, render=False)
        state.update(left=1, guided=0)
        worker.run, route._finish_guided = fake_worker, spy_finish
        try:
            return call("pcb_route", project=d, ignore_nets=["GND"], render=False, **kw)[0], d
        finally:
            worker.run, route._finish_guided = real_worker, real_finish

    s, d = run("finish kept")
    kinds = [r.get("strategy") for r in s["rounds"] if r.get("strategy")]
    assert kinds[:3] == ["route", "continue", "guided"] and state["guided"] == 1, s["rounds"]
    assert s["guided_finish"]["routed"] == 1 and "guided" in s["router"], s
    assert state["plans"][-1][0] == ["F.Cu", "B.Cu"] and state["plans"][-1][1] == ["GND"], state["plans"]
    assert {"kept_round": 3} in s["rounds"] and "ripup" not in kinds, s["rounds"]
    assert any("guided router finished 1" in c for c in s["changed"]), s["changed"]
    assert s["verify"]["drc"]["errors"] == 0, s["verify"]

    state["errors"] = 99                       # the finisher "adds DRC errors": discarded, Freerouting's result stands
    try:
        s, d = run("finish discarded")
    finally:
        state.pop("errors")
    g = [r for r in s["rounds"] if r.get("strategy") == "guided"]
    assert g and g[0].get("discarded") == "added DRC errors" and not s.get("guided_finish"), s["rounds"]
    assert "guided" not in s["router"] and s["rounds"][-1]["kept_round"] != g[0]["round"], s

    s, d = run("finish off", finish=False)
    assert state["guided"] == 0 and not any(r.get("strategy") == "guided" for r in s["rounds"]), s["rounds"]


if __name__ == "__main__":
    import time
    import traceback
    fails = 0
    only = sys.argv[1:]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t:.1f}s)")
            except Exception as e:  # noqa: BLE001
                fails += 1
                traceback.print_exc()
                print(f"FAIL {name}: {e}")
    print(f"temp dir: {_TMP}")
    sys.exit(1 if fails else 0)
