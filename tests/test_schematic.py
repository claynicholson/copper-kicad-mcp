"""Schematic engine tests — run against the real KiCad 10 install (kicad-cli + stock libraries).

    python -m pytest tests/test_schematic.py      (pytest optional)
    python tests/test_schematic.py                (plain runner)

Every connectivity claim is checked with KiCad's own netlist / ERC, never with Copper's model.
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
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-home-"))

from copper_kicad import sexpr  # noqa: E402
from copper_kicad.kicad import env, libs  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import layout as LAY  # noqa: E402
from copper_kicad.sch import netlist as N  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch import symbols as G  # noqa: E402
from copper_kicad.sch.model import Schematic  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="copper-sch-test-"))


def _project(name: str) -> Project:
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return Project(d, name)


def _erc(pr: Project) -> list[dict]:
    out = pr.dir / "erc.json"
    env.run(["sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(pr.sch)])
    data = json.loads(out.read_text(encoding="utf-8"))
    return [v for s in data["sheets"] for v in s["violations"]]


# library-table warnings depend on the machine's KiCad config, not on our wiring
_ENV_WARNINGS = {"lib_symbol_issues", "footprint_link_issues", "lib_symbol_mismatch"}


def _wiring_errors(pr):
    return [v for v in _erc(pr) if v["severity"] == "error" and v["type"] not in _ENV_WARNINGS]


def _overlaps(sch: Schematic) -> list[tuple]:
    """Pairs of overlapping items: symbol bodies vs symbol bodies, and labels vs symbol bodies."""
    syms = [s for s in sch.symbols() if not s.is_power]
    bodies = []
    for s in syms:
        ls = sch.lib_symbol(s.lib_name)
        gb = libs.unit_bbox(ls, s.unit, s.body_style, include_pins=False)
        bodies.append((f"{s.ref}/{s.unit}", G.bbox_world(s.at, s.matrix, gb)))
    bad = []
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            if LAY._rect_hit(bodies[i][1], bodies[j][1], 0.5):
                bad.append((bodies[i][0], bodies[j][0]))
    occ = LAY.occupied(sch)
    labels = [b for b in occ if b not in [x[1] for x in bodies]]
    for l in sch.labels():
        x, y = l["at"]
        for name, bb in bodies:
            if bb[0] + 0.3 < x < bb[2] - 0.3 and bb[1] + 0.3 < y < bb[3] - 0.3:
                bad.append((f"label {l['net']}", name))
    for s in sch.symbols():
        if not s.is_power:
            continue
        x, y = s.at
        for name, bb in bodies:
            if bb[0] + 0.3 < x < bb[2] - 0.3 and bb[1] + 0.3 < y < bb[3] - 0.3:
                bad.append((f"power {s.value}", name))
    return bad


# ── geometry ──────────────────────────────────────────────────────────────
def test_pin_transforms_all_orientations_match_kicad_netlist():
    """Labels dropped at Copper-computed pin tips must land on the pins in KiCad's netlist,
    for every rotation × mirror, on symbols with pins on all four sides."""
    pr = _project("orient")
    s = Schematic.new(pr.sch, pr.name, "A3")
    expect = {}
    i = 0
    for lid in ("Regulator_Linear:AMS1117-3.3", "MCU_Microchip_ATtiny:ATtiny85-20S", "Connector:USB_C_Receptacle_USB2.0_16P"):
        L = libs.read_symbol(lid)
        key = s.embed(L)
        for rot in (0, 90, 180, 270):
            for mir in (None, "x", "y"):
                i += 1
                ref = f"U{i}"
                si = s.add_symbol(key, lid, ref, G.snap_pt([30 + (i % 7) * 50, 30 + (i // 7) * 50]), rot, mir)
                seen = set()
                for p in si.pins_world():
                    k = tuple(p["xy"])
                    if k in seen:   # stacked pins share a tip
                        continue
                    seen.add(k)
                    s.add_label(f"N{i}_{p['number']}", p["xy"], 0)
                    expect[(ref, p["number"])] = f"N{i}_{p['number']}"
    s.save(pr.sch)
    pn = N.pin_nets(N.export_netlist(pr))
    bad = [(k, v, pn.get(k)) for k, v in expect.items() if N.short_name(pn.get(k, "")) != v]
    assert len(expect) > 150
    assert not bad, bad[:5]


def test_negative_control_wrong_mirror_is_detected():
    """Sanity: swapping the mirror matrices must break connectivity (the proof above is discriminating)."""
    m_ok = G.matrix(0, "x")
    m_bad = G.matrix(0, "y")
    p = {"at": [7.62, 2.54], "angle": 180}
    assert G.pin_tip([0, 0], m_ok, p) != G.pin_tip([0, 0], m_bad, p)
    # orientation 0: lib Y-up → sheet Y-down
    assert G.pin_tip([100, 100], G.matrix(0), {"at": [0, 5.08], "angle": 270}) == [100, 94.92]
    assert G.pin_outward(G.matrix(0), {"angle": 0}) == (-1, 0)        # pin pointing right sits on the left side
    assert G.pin_outward(G.matrix(90), {"angle": 0}) == (0, 1)


def test_pin_lookup_normalization():
    pins = [{"number": "1", "name": "VBUS", "alternates": []},
            {"number": "2", "name": "~{RESET}", "alternates": []},
            {"number": "3", "name": "IO20/RXD", "alternates": []},
            {"number": "4", "name": "PA9", "alternates": ["USART1_TX", "TIM1_CH2"]},
            {"number": "5", "name": "PB6", "alternates": ["USART1_TX", "I2C1_SCL"]},
            {"number": "6", "name": "GND", "alternates": []},
            {"number": "7", "name": "GND", "alternates": []},
            {"number": "8", "name": "PA10", "alternates": ["USART1_RX"]}]
    assert [p["number"] for p in G.match_pins(pins, "1")[0]] == ["1"]
    assert G.match_pins(pins, "VBUS")[1] == "name"
    assert G.match_pins(pins, "vbus")[1] == "name-ci"
    assert [p["number"] for p in G.match_pins(pins, "RESET")[0]] == ["2"]
    assert [p["number"] for p in G.match_pins(pins, "~{RESET}")[0]] == ["2"]
    assert [p["number"] for p in G.match_pins(pins, "RXD")[0]] == ["3"]
    assert [p["number"] for p in G.match_pins(pins, "GND")[0]] == ["6", "7"]
    r, how = G.match_pins(pins, "USART1_RX")
    assert how == "alternate" and r[0]["number"] == "8" and r[0]["alternate"] == "USART1_RX"
    assert G.match_pins(pins, "USART1_TX")[1] == "ambiguous"
    assert G.match_pins(pins, "NOPE") == ([], "")
    assert G.match_pins([{"number": "1", "name": "~", "alternates": []}], "x")[1] == "single-pin"


# ── libraries ─────────────────────────────────────────────────────────────
def test_library_search_and_flattening():
    r = libs.search_symbols("AMS1117-3.3", limit=3)
    assert r[0]["lib_id"] == "Regulator_Linear:AMS1117-3.3"
    assert libs.search_symbols("Device:R", limit=1)[0]["lib_id"] == "Device:R"
    assert libs.search_symbols("GND", limit=1)[0]["lib_id"] == "power:GND"
    s = libs.read_symbol("Regulator_Linear:AMS1117-3.3")
    d = s.to_dict()
    assert d["extends"] == "Regulator_Linear:AP1117-15"          # derived symbol …
    assert {p["name"] for p in d["pins"]} == {"GND", "VI", "VO"}   # … flattened with the parent's pins
    assert d["value"] == "AMS1117-3.3" and "SOT-223" in d["footprint"]
    assert all(str(u[1]).startswith("AMS1117-3.3_") for u in sexpr.find_all(s.tree, "symbol"))
    assert libs.read_symbol("Amplifier_Operational:LM358").unit_count == 3
    try:
        libs.read_symbol("Device:NoSuchPart")
        assert False
    except ToolError as e:
        assert e.code == "SYMBOL_NOT_FOUND"
    f = libs.search_footprints("R_0603", limit=1)[0]
    assert f["fp_id"] == "Resistor_SMD:R_0603_1608Metric"
    fp = libs.read_footprint("Package_TO_SOT_SMD:SOT-223-3_TabPin2")
    assert fp["pad_count"] == 3 and fp["courtyard"]
    rep = libs.repair_tables(dry_run=True)
    assert rep["dry_run"] is True


# ── file format ──────────────────────────────────────────────────────────
def test_save_is_lossless_on_kicad_files():
    """Load + save (+ KiCad canonicalisation) of a KiCad-written demo must be byte-identical."""
    inst = env.kicad()
    demo = inst.share / "demos" / "stickhub"
    pr = _project("StickHub")
    for f in demo.iterdir():
        if f.is_file():
            shutil.copy2(f, pr.dir / f.name)
    env.run(["sch", "upgrade", "--force", str(pr.sch)])
    ref = pr.sch.read_bytes()
    s = Schematic.load(pr.sch, pr.name)
    s.save(pr.sch)
    assert pr.sch.read_bytes() == ref


def test_new_schematic_header():
    pr = _project("blank")
    O.apply_ops(pr, [{"op": "text", "text": "hello", "at": [50, 50]}])
    t = sexpr.loads(pr.sch.read_text(encoding="utf-8"))
    assert int(sexpr.value(t, "version")) >= 20260306
    assert sexpr.value(t, "generator") == "eeschema"
    assert pr.pro.exists()


# ── the killer scenario ─────────────────────────────────────────────────
R06 = "Resistor_SMD:R_0603_1608Metric"
C06 = "Capacitor_SMD:C_0603_1608Metric"
KILLER = [
    {"op": "place", "lib_id": "RF_Module:ESP32-C3-WROOM-02", "ref": "U1"},
    {"op": "place", "lib_id": "Regulator_Linear:AMS1117-3.3", "ref": "U2"},
    {"op": "place", "lib_id": "Connector:USB_C_Receptacle_USB2.0_16P", "ref": "J1",
     "footprint": "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"},
    {"op": "place", "lib_id": "Device:C", "value": "10uF", "footprint": C06, "id": "cin"},
    {"op": "place", "lib_id": "Device:C", "value": "10uF", "footprint": C06, "id": "cout"},
    {"op": "place", "lib_id": "Device:C", "value": "100nF", "footprint": C06, "id": "cdec"},
    {"op": "place", "lib_id": "Device:R", "value": "10k", "footprint": R06, "id": "ren"},
    {"op": "place", "lib_id": "Device:C", "value": "1uF", "footprint": C06, "id": "cen"},
    {"op": "place", "lib_id": "Device:R", "value": "5.1k", "footprint": R06, "id": "rcc1"},
    {"op": "place", "lib_id": "Device:R", "value": "5.1k", "footprint": R06, "id": "rcc2"},
    {"op": "place", "lib_id": "Device:R", "value": "1k", "footprint": R06, "id": "rled"},
    {"op": "place", "lib_id": "Device:LED", "value": "LED red", "footprint": "LED_SMD:LED_0603_1608Metric", "id": "led"},
    {"op": "connect", "pins": ["J1.VBUS", "U2.VI", "cin.1"], "net": "VBUS"},
    {"op": "connect", "pins": ["J1.GND", "J1.SHIELD", "U2.GND", "cin.2", "cout.2", "cdec.2", "cen.2", "rcc1.2", "rcc2.2",
                               "led.K", "U1.GND"], "net": "GND"},
    {"op": "connect", "pins": ["U2.VO", "cout.1", "cdec.1", "U1.3V3", "ren.1"], "net": "+3V3"},
    {"op": "connect", "pins": ["ren.2", "cen.1", "U1.EN"], "net": "EN"},
    {"op": "connect", "pins": ["J1.CC1", "rcc1.1"], "net": "CC1"},
    {"op": "connect", "pins": ["J1.CC2", "rcc2.1"], "net": "CC2"},
    {"op": "connect", "pins": ["J1.D+", "U1.IO19"], "net": "USB_DP"},
    {"op": "connect", "pins": ["J1.D-", "U1.IO18"], "net": "USB_DN"},
    {"op": "connect", "pins": ["U1.IO8", "rled.1"], "net": "LED_DRV"},
    {"op": "connect", "pins": ["rled.2", "led.A"], "net": "LED_A"},
    {"op": "no_connect", "pins": ["J1.SBU1", "J1.SBU2", "U1.IO0", "U1.IO1", "U1.IO2", "U1.IO3", "U1.IO4", "U1.IO5",
                                  "U1.IO6", "U1.IO7", "U1.IO9", "U1.IO10", "U1.TXD", "U1.RXD"]},
    {"op": "pwr_flag", "net": "GND"},
    {"op": "pwr_flag", "net": "VBUS"},
]


def test_killer_scenario_empty_project_to_clean_erc():
    pr = _project("killer")
    r = O.apply_ops(pr, KILLER)
    assert r["refs"]["led"] == "D1" and r["refs"]["cin"].startswith("C")
    nl = N.export_netlist(pr)
    chk = N.check_expectations(nl, r["expect"])
    assert chk["checked"] >= 40 and not chk["mismatches"], chk["mismatches"]
    nets = {N.short_name(k): {f"{n['ref']}.{n['pin']}" for n in v} for k, v in nl["nets"].items()}
    assert {"J1.A6", "J1.B6", "U1.14"} <= nets["USB_DP"]
    assert {"U2.2", "U1.1"} <= nets["+3V3"]
    assert {"J1.A4", "J1.A9", "J1.B4", "J1.B9", "U2.3"} <= nets["VBUS"]
    errs = _wiring_errors(pr)
    assert not errs, [(v["type"], [i["description"] for i in v["items"]]) for v in errs]
    warns = [v for v in _erc(pr) if v["type"] not in _ENV_WARNINGS]
    assert not warns, [(v["type"], v["description"]) for v in warns]
    sch = Schematic.load(pr.sch, pr.name)
    assert not _overlaps(sch), _overlaps(sch)
    # all symbols inside the page and on the 1.27 mm grid
    w, h = sch.page_size()
    for s in sch.symbols():
        x, y = s.at
        assert 0 < x < w and 0 < y < h
        assert abs(x / 1.27 - round(x / 1.27)) < 1e-3 and abs(y / 1.27 - round(y / 1.27)) < 1e-3
    # chain R→LED got a real wire, not two dangling labels
    assert any("wired as LED_A" in c for c in r["changed"])
    from copper_kicad import render
    imgs = render.schematic(pr.sch, pr.dir / "png", width=1200)
    assert imgs and Path(imgs[0].path).stat().st_size > 10000


# ── edits ─────────────────────────────────────────────────────────────────
def _base(name):
    pr = _project(name)
    O.apply_ops(pr, [
        {"op": "place", "lib_id": "Amplifier_Operational:LM358", "ref": "U1", "at": [100.33, 60.96]},
        {"op": "place", "lib_id": "Device:R", "ref": "R1", "value": "10k", "at": [60.96, 60.96]},
        {"op": "place", "lib_id": "Device:C", "ref": "C1", "value": "100nF", "at": [60.96, 91.44]},
        {"op": "connect", "pins": ["U1.V+", "R1.1"], "net": "+5V"},
        {"op": "connect", "pins": ["U1.V-", "C1.2"], "net": "GND"},
        {"op": "connect", "pins": ["U1.3", "R1.2", "C1.1"], "net": "IN"},
    ])
    return pr


def test_multi_unit_place_all_and_unit_aware_pins():
    pr = _base("multiunit")
    sch = Schematic.load(pr.sch, pr.name)
    assert sorted(s.unit for s in sch.find_ref("U1")) == [1, 2, 3]
    pn = N.pin_nets(N.export_netlist(pr))
    assert N.short_name(pn[("U1", "8")]) == "+5V" and N.short_name(pn[("U1", "4")]) == "GND"
    assert N.short_name(pn[("U1", "3")]) == "IN" and N.short_name(pn[("C1", "1")]) == "IN"


def test_move_rotate_keeps_connectivity_and_refuses_shorts():
    pr = _base("move")
    before = N.pin_nets(N.export_netlist(pr))
    r = O.apply_ops(pr, [{"op": "move", "ref": "R1", "at": [40.64, 50.8], "rot": 90},
                         {"op": "move", "ref": "C1", "by": [5.08, 0], "mirror": "y"}])
    after = N.pin_nets(N.export_netlist(pr))
    for k in (("R1", "1"), ("R1", "2"), ("C1", "1"), ("C1", "2")):
        assert N.short_name(after[k]) == N.short_name(before[k]), (k, before[k], after[k])
    sch = Schematic.load(pr.sch, pr.name)
    assert sch.find_ref("R1")[0].rot == 90 and sch.find_ref("C1")[0].mirror == "y"
    # connecting an already-connected pin to another net is refused, atomically
    rev = pr.sch.read_bytes()
    try:
        O.apply_ops(pr, [{"op": "connect", "pin": "R1.2", "net": "OTHER"}])
        assert False, "should refuse"
    except ToolError as e:
        assert e.code == "NET_CONFLICT"
    assert pr.sch.read_bytes() == rev
    # idempotent re-connect
    r = O.apply_ops(pr, [{"op": "connect", "pin": "R1.2", "net": "IN"}])
    assert "already" in r["changed"][0]


def test_atomic_batch_and_dry_run():
    pr = _base("atomic")
    rev = pr.sch.read_bytes()
    try:
        O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R9"},
                         {"op": "connect", "pin": "U1.NOPE", "net": "X"}])
        assert False
    except ToolError as e:
        assert e.code == "PIN_NOT_FOUND" and "nothing was written" in e.message
    assert pr.sch.read_bytes() == rev
    r = O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R9"}], dry_run=True)
    assert r["dry_run"] and pr.sch.read_bytes() == rev
    try:
        O.apply_ops(pr, [{"op": "plac", "lib_id": "Device:R"}])
        assert False
    except ToolError as e:
        assert e.code == "BAD_OP"


def test_wire_disconnect_delete_swap():
    pr = _base("edit")
    r = O.apply_ops(pr, [{"op": "wire", "from": "U1.1", "to": "U1.2"}])
    sch = Schematic.load(pr.sch, pr.name)
    # the feedback wire must not cut through the op-amp body
    u = [s for s in sch.find_ref("U1") if s.unit == 1][0]
    gb = G.bbox_world(u.at, u.matrix, libs.unit_bbox(sch.lib_symbol(u.lib_name), 1, 1, include_pins=False))
    for w in sch.wires():
        assert not O._seg_hits_box(w["a"], w["b"], gb), w
    pn = N.pin_nets(N.export_netlist(pr))
    assert pn[("U1", "1")] == pn[("U1", "2")]
    r = O.apply_ops(pr, [{"op": "wire", "from": "U1.1", "to": "U1.2"}])
    assert "already wired" in r["changed"][0]
    O.apply_ops(pr, [{"op": "disconnect", "pin": "U1.1"}])
    pn = N.pin_nets(N.export_netlist(pr))
    assert pn[("U1", "1")] != pn[("U1", "2")]
    assert not Schematic.load(pr.sch, pr.name).wires() or all(
        not (w["a"] == w["b"]) for w in Schematic.load(pr.sch, pr.name).wires())
    O.apply_ops(pr, [{"op": "swap", "ref": "R1", "lib_id": "Device:R_Small", "footprint": R06},
                     {"op": "set_field", "ref": "R1", "field": "LCSC", "value": "C25804"}])
    nl = N.export_netlist(pr)
    pn = N.pin_nets(nl)
    assert nl["components"]["R1"]["lib_id"] == "Device:R_Small"
    assert nl["components"]["R1"]["fields"].get("LCSC") == "C25804"
    assert N.short_name(pn[("R1", "1")]) == "+5V" and N.short_name(pn[("R1", "2")]) == "IN"
    O.apply_ops(pr, [{"op": "delete", "ref": "C1"}])
    sch = Schematic.load(pr.sch, pr.name)
    assert not sch.find_ref("C1")
    assert sum(1 for l in sch.labels() if l["net"] == "IN") == 2   # C1's label went with it


def test_tidy_relayout_preserves_connectivity():
    pr = _project("tidy")
    # a deliberately messy sheet: parts piled up in a corner
    ops = [{"op": "place", "lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S", "ref": "U1", "at": [40.64, 40.64]},
           {"op": "place", "lib_id": "Device:C", "ref": "C1", "value": "100nF", "at": [63.5, 30.48]},
           {"op": "place", "lib_id": "Device:R", "ref": "R1", "value": "330", "at": [68.58, 30.48]},
           {"op": "place", "lib_id": "Device:LED", "ref": "D1", "at": [76.2, 30.48]},
           {"op": "connect", "pins": ["U1.VCC", "C1.1"], "net": "+5V"},
           {"op": "connect", "pins": ["U1.GND", "C1.2", "D1.K"], "net": "GND"},
           {"op": "connect", "pins": ["U1.PB0", "R1.1"], "net": "LED"},
           {"op": "wire", "from": "R1.2", "to": "D1.A"},
           {"op": "no_connect", "pins": ["U1.PB1", "U1.PB2", "U1.PB3", "U1.PB4", "U1.PB5"]}]
    O.apply_ops(pr, ops)
    before = O._partition(N.export_netlist(pr))
    r = LAY.tidy(pr)
    assert r["connectivity"].startswith("identical")
    after = O._partition(N.export_netlist(pr))
    assert before == after
    sch = Schematic.load(pr.sch, pr.name)
    assert not _overlaps(sch), _overlaps(sch)
    assert not _wiring_errors(pr) or all(v["type"] in ("power_pin_not_driven",) for v in _wiring_errors(pr))


def test_tools_registered_and_roundtrip():
    from copper_kicad.mcp import registry
    import copper_kicad.tools  # noqa: F401
    for name in ("sch_apply", "sch_read", "sch_tidy", "library"):
        assert name in registry.TOOLS, name
    assert registry.TOOLS["sch_apply"].core and registry.TOOLS["sch_read"].core
    pr = _base("tools")
    out = registry.call_tool("sch_read", {"project": str(pr.pro), "query": "pins", "ref": "U1", "filter": "V+"})
    assert not out["isError"], out
    d = out["structuredContent"]
    assert d["pins"][0]["net"] == "+5V"
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "preview": False,
                                           "ops": [{"op": "no_connect", "pins": ["U1.5", "U1.6", "U1.7"]}]})
    assert not out["isError"], out["content"][0]["text"]
    d = out["structuredContent"]
    assert d["connectivity"]["ok"] == d["connectivity"]["checked"]
    out = registry.call_tool("library", {"action": "symbol_info", "lib_id": "Device:R"})
    assert not out["isError"] and out["structuredContent"]["pin_count"] == 2


def test_hierarchical_subsheet_refs_unique_per_instance():
    """Placing on a sub-sheet used twice (KiCad demo complex_hierarchy) gives one unique ref per instance."""
    inst = env.kicad()
    pr = _project("complex_hierarchy")
    shutil.rmtree(pr.dir)
    shutil.copytree(inst.share / "demos" / "complex_hierarchy", pr.dir)
    before = N.export_netlist(pr)["components"]
    r = O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "id": "rx", "value": "47k", "sheet": "ampli_ht.kicad_sch"},
                         {"op": "connect", "pin": "rx.1", "net": "TESTNET", "sheet": "ampli_ht.kicad_sch"},
                         {"op": "connect", "pin": "rx.2", "net": "GND", "sheet": "ampli_ht.kicad_sch"}])
    nl = N.export_netlist(pr)
    new = {k: v for k, v in nl["components"].items() if k not in before}
    assert len(new) == 2 and {v["sheet"] for v in new.values()} == {"/ampli_ht_vertical/", "/ampli_ht_horizontal/"}
    assert not N.check_expectations(nl, r["expect"])["mismatches"]


def test_incremental_batch_on_existing_sheet():
    pr = _project("incr")
    O.apply_ops(pr, KILLER)
    r = O.apply_ops(pr, [
        {"op": "disconnect", "pins": ["U1.IO4", "U1.IO5"]},
        {"op": "place", "lib_id": "Device:R", "value": "4.7k", "footprint": R06, "id": "rsda"},
        {"op": "place", "lib_id": "Device:R", "value": "4.7k", "footprint": R06, "id": "rscl"},
        {"op": "place", "lib_id": "Connector_Generic:Conn_01x04", "id": "jq", "value": "I2C"},
        {"op": "connect", "pins": ["U1.IO4", "rsda.2", "jq.3"], "net": "I2C_SDA"},
        {"op": "connect", "pins": ["U1.IO5", "rscl.2", "jq.4"], "net": "I2C_SCL"},
        {"op": "connect", "pins": ["rsda.1", "rscl.1", "jq.2"], "net": "+3V3"},
        {"op": "connect", "pin": "jq.1", "net": "GND"}])
    assert r["refs"] == {"rsda": "R5", "rscl": "R6", "jq": "J2"}
    assert not N.check_expectations(N.export_netlist(pr), r["expect"])["mismatches"]
    assert not _wiring_errors(pr)
    sch = Schematic.load(pr.sch, pr.name)
    assert not _overlaps(sch), _overlaps(sch)


# ── regressions found by the skeptical-tester pass ────────────────────────
def _demo(dname: str, name: str | None = None) -> Project:
    src = env.kicad().share / "demos" / dname
    d = TMP / (name or dname)
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(src, d)
    return Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)


def _code(fn):
    try:
        fn()
    except ToolError as e:
        return e.code
    return None


def test_subsheet_edit_refused_while_root_open_in_kicad():
    """eeschema only locks the ROOT file of an open hierarchy; sub-sheet edits must honour it."""
    pr = _demo("complex_hierarchy", "lock_sub")
    sub = pr.dir / "ampli_ht.kicad_sch"
    before = sub.read_bytes()
    (pr.dir / f"~{pr.sch.name}.lck").write_text("{}")
    assert _code(lambda: O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "sheet": "ampli_ht.kicad_sch"}])) \
        == "FILE_OPEN_IN_KICAD"
    assert sub.read_bytes() == before
    # explicit override still works (sheet looked up by name, any case)
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "sheet": "AMPLI_HT"}], while_open="revert")


def test_place_id_validation():
    pr = _project("ids")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [50.8, 50.8]}])
    rev = pr.sch.read_bytes()
    # an id equal to an existing reference used to silently hijack 'R1.x' pin tokens
    assert _code(lambda: O.apply_ops(pr, [{"op": "place", "lib_id": "Device:C", "id": "R1"},
                                          {"op": "connect", "pin": "R1.1", "net": "FOO"}])) == "DUPLICATE_ID"
    assert _code(lambda: O.apply_ops(pr, [{"op": "place", "lib_id": "Device:C", "id": "x"},
                                          {"op": "place", "lib_id": "Device:C", "id": "x"}])) == "DUPLICATE_ID"
    assert _code(lambda: O.apply_ops(pr, [{"op": "place", "lib_id": "Device:C", "id": "my.cap"}])) == "BAD_OP"
    assert pr.sch.read_bytes() == rev


def test_net_name_case_mismatch_refused():
    pr = _base("netcase")
    for net in ("gnd", "+5v"):
        assert _code(lambda: O.apply_ops(pr, [{"op": "connect", "pin": "R1.2", "net": net}], dry_run=True)) \
            == "NET_NAME_CASE", net
    # existing local label 'IN'
    assert _code(lambda: O.apply_ops(pr, [{"op": "connect", "pin": "U1.5", "net": "in"}], dry_run=True)) \
        == "NET_NAME_CASE"


def test_pwr_flag_is_idempotent():
    pr = _project("flag2")
    ops = [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [50.8, 50.8]},
           {"op": "connect", "pin": "R1.1", "net": "GND"}, {"op": "connect", "pin": "R1.2", "net": "VBUS"},
           {"op": "pwr_flag", "net": "GND"}, {"op": "pwr_flag", "net": "VBUS"}]
    O.apply_ops(pr, ops)
    r = O.apply_ops(pr, [{"op": "pwr_flag", "net": "GND"}, {"op": "pwr_flag", "net": "VBUS"}])
    assert all("already has PWR_FLAG" in c for c in r["changed"]), r["changed"]
    sch = Schematic.load(pr.sch, pr.name)
    assert sum(1 for s in sch.symbols() if s.lib_id.endswith("PWR_FLAG")) == 2
    assert not [v for v in _wiring_errors(pr) if v["type"] == "pin_to_pin"]


def test_move_or_place_onto_foreign_pin_is_refused():
    pr = _project("overlap")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [50.8, 50.8]},
                     {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [76.2, 50.8]},
                     {"op": "connect", "pin": "R1.1", "net": "A"}, {"op": "connect", "pin": "R2.2", "net": "B"}])
    rev = pr.sch.read_bytes()
    # R2.1 would land exactly on R1.2 at (50.8, 54.61): a silent short before the fix
    assert _code(lambda: O.apply_ops(pr, [{"op": "move", "ref": "R2", "at": [50.8, 58.42]}])) == "PIN_OVERLAP"
    assert _code(lambda: O.apply_ops(pr, [{"op": "place", "lib_id": "Device:C", "at": [50.8, 58.42]}])) == "PIN_OVERLAP"
    assert pr.sch.read_bytes() == rev
    pn = N.pin_nets(N.export_netlist(pr))
    assert pn[("R1", "2")] != pn[("R2", "1")]
    # rotating in place is still fine (its own label stubs are re-attached)
    O.apply_ops(pr, [{"op": "move", "ref": "R2", "rot": 180}])
    assert N.short_name(N.pin_nets(N.export_netlist(pr))[("R2", "2")]) == "B"


def test_readonly_file_gives_actionable_error():
    import stat
    pr = _project("ro")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [50.8, 50.8]}])
    os.chmod(pr.sch, stat.S_IREAD)
    try:
        assert _code(lambda: O.apply_ops(pr, [{"op": "connect", "pin": "R1.1", "net": "X"}])) == "WRITE_FAILED"
    finally:
        os.chmod(pr.sch, stat.S_IREAD | stat.S_IWRITE)


def test_tidy_on_real_handdrawn_demos():
    """ecc83: an auto-named 3-pin net used to split (wired pair + labelled 3rd pin) → tidy refused;
    StickHub: custom power lib 'RobotProtos:VIN' used to crash looking for 'power:VIN'."""
    pr = _demo("ecc83", "ecc83_tidy")
    before = O._partition(N.export_netlist(pr))
    r = LAY.tidy(pr)
    assert r["connectivity"].startswith("identical")
    assert O._partition(N.export_netlist(pr)) == before
    pr = _demo("stickhub", "stickhub_tidy")
    r = LAY.tidy(pr, dry_run=True)
    assert r["connectivity"].startswith("identical")


def test_connect_reuses_existing_custom_power_symbol():
    pr = _demo("stickhub", "stickhub_vin")
    r = O.apply_ops(pr, [{"op": "place", "lib_id": "Device:C", "id": "c", "value": "1uF"},
                         {"op": "connect", "pin": "c.1", "net": "VIN"}, {"op": "connect", "pin": "c.2", "net": "GND"}])
    assert any("power symbol" in c and "VIN" in c for c in r["changed"]), r["changed"]
    sch = Schematic.load(pr.sch, pr.name)
    vins = {s.lib_id for s in sch.symbols() if s.is_power and s.value == "VIN"}
    assert vins == {"RobotProtos:VIN"}, vins
    assert not N.check_expectations(N.export_netlist(pr), r["expect"])["mismatches"]


def test_sheet_lookup_and_ref_hints():
    pr = _demo("multichannel", "mc")
    f, path = O.sheet_file(pr, "ch2")                          # sheet NAME, wrong case
    assert f.name == "channel_strip.kicad_sch" and path.count("/") == 2
    assert O.sheet_file(pr, "CHANNEL_STRIP.KICAD_SCH")[0].name == "channel_strip.kicad_sch"
    assert O.sheet_file(pr, str(pr.sch))[0] == pr.sch
    assert _code(lambda: O.sheet_file(pr, "nope.kicad_sch")) == "SHEET_NOT_FOUND"
    sch = Schematic.load(pr.sch, pr.name)
    sub_only = [r for r in sorted(N.export_netlist(pr)["components"]) if not sch.find_ref(r)]
    assert sub_only
    try:
        O.apply_ops(pr, [{"op": "connect", "pin": f"{sub_only[0]}.1", "net": "X"}], dry_run=True)
        assert False
    except ToolError as e:
        assert e.code == "REF_NOT_FOUND" and "sheet=" in (e.fix or ""), e.fix


def test_hierarchical_label_without_sheet_pin_warns():
    pr = _demo("multichannel", "mc_h")
    r = O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "id": "r"},
                         {"op": "connect", "pin": "r.1", "net": "NEW_HIER", "kind": "hierarchical"}], sheet="CH1",
                    dry_run=True)
    assert any("no matching sheet pin" in w for w in r["warnings"]), r["warnings"]


def test_planned_nets_fast_on_big_existing_sheet():
    """Auto-placing into an existing sheet used to walk wires per pin (~25 s on StickHub)."""
    import time
    pr = _demo("stickhub", "stickhub_perf")
    t = time.time()
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "id": "r"}, {"op": "connect", "pin": "r.1", "net": "LED5"}],
                dry_run=True)
    assert time.time() - t < 8, time.time() - t


def test_erc_output_is_compact_and_actionable():
    from copper_kicad.tools import sch_tools as ST
    raw = {"errors": 2, "warnings": 9, "first_run": True,
           "by_type": {"power_pin_not_driven": 2, "lib_symbol_issues": 9},
           "top": [{"severity": "error", "type": "power_pin_not_driven", "message": "x - #PWR01.1 (net VBUS)", "fix": "?"},
                   {"severity": "error", "type": "power_pin_not_driven", "message": "x - #PWR03.1 (net GND)", "fix": "?"}]
           + [{"severity": "warning", "type": "lib_symbol_issues", "message": f"lib {i}", "fix": "repair"} for i in range(9)]}
    d = ST.compact_erc(raw)
    assert len(d["issues"]) == 1 and d["issues"][0]["count"] == 2
    fix = d["issues"][0]["fix"]
    assert '"op":"pwr_flag","net":"VBUS"' in fix and '"net":"GND"' in fix
    assert d["environment"]["count"] == 9 and "baseline" in d
    # the cropped preview is rendered at a legible scale (it used to come back ~230 px wide)
    pr = _base("preview")
    imgs = ST._preview(pr, pr.sch, [60, 55, 75, 70])
    from PIL import Image as PImage
    assert PImage.open(imgs[0].path).size[0] >= 800


# ── regressions from the multi-tester dogfood (stm32-sensor, power-board, edit-existing, rp2040) ─────────
def _flags(path) -> int:
    sch = Schematic.load(path)
    return sum(1 for s in sch.symbols() if O.is_flag(s))


def test_tidy_keeps_pwr_flags():
    """sch_tidy used to delete every PWR_FLAG (they are not in KiCad's netlist) yet claim 'connectivity identical'."""
    pr = _project("tidy_flags")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Regulator_Linear:AMS1117-3.3", "ref": "U1", "at": [100.33, 60.96]},
                     {"op": "place", "lib_id": "Device:C", "ref": "C1", "value": "10uF", "at": [60.96, 60.96]},
                     {"op": "place", "lib_id": "Device:R", "ref": "R1", "value": "1k", "at": [60.96, 91.44]},
                     {"op": "connect", "pins": ["U1.VI", "C1.1"], "net": "VBAT"},
                     {"op": "connect", "pins": ["U1.GND", "C1.2", "R1.2"], "net": "GND"},
                     {"op": "connect", "pins": ["U1.VO", "R1.1"], "net": "+3V3"},
                     {"op": "pwr_flag", "net": "GND"}, {"op": "pwr_flag", "net": "VBAT"}])
    assert _flags(pr.sch) == 2 and not _wiring_errors(pr)
    r = LAY.tidy(pr)
    assert "2 PWR_FLAG" in r["connectivity"], r["connectivity"]
    assert _flags(pr.sch) == 2
    errs = _wiring_errors(pr)
    assert not errs, [(v["type"], v["description"]) for v in errs]
    # sch_tidy (the tool) runs ERC itself and reports it
    from copper_kicad.mcp import registry
    import copper_kicad.tools  # noqa: F401
    out = registry.call_tool("sch_tidy", {"project": str(pr.pro), "preview": False})
    assert not out["isError"], out["content"][0]["text"]
    assert out["structuredContent"]["verify"]["erc"]["errors"] == 0


def test_tidy_subsheet_keeps_hierarchical_labels():
    """pic_sockets: tidy used to delete the 4 hierarchical labels + the PWR_FLAG, cutting the sheet off its parent."""
    pr = _demo("pic_programmer", "pic_tidy")
    full = lambda: O._partition(N.export_netlist(pr))  # noqa: E731
    before = full()
    r = LAY.tidy(pr, "pic_sockets")
    assert "4 hierarchical/global" in r["connectivity"] and "1 PWR_FLAG" in r["connectivity"], r["connectivity"]
    assert full() == before                        # the WHOLE hierarchy, not only the isolated sub-sheet
    sch = Schematic.load(pr.dir / "pic_sockets.kicad_sch")
    assert {l["net"] for l in sch.labels() if l["kind"] == "hierarchical"} == {"VCC_PIC", "VPP-MCLR", "CLOCK-RB6", "DATA-RB7"}
    assert _flags(pr.dir / "pic_sockets.kicad_sch") == 1
    assert not _wiring_errors(pr), [(v["type"], v["description"]) for v in _wiring_errors(pr)]


def test_tidy_refuses_multi_instance_sheet():
    """complex_hierarchy ampli_ht is used twice: auto-named nets would be labelled after one instance's refs."""
    pr = _demo("complex_hierarchy", "cx_tidy")
    before = (pr.dir / "ampli_ht.kicad_sch").read_bytes()
    assert _code(lambda: LAY.tidy(pr, "ampli_ht.kicad_sch")) == "TIDY_UNSUPPORTED"
    assert (pr.dir / "ampli_ht.kicad_sch").read_bytes() == before


def test_sheet_path_net_names_are_normalised():
    """ERC hints said pwr_flag net '/DW_VCC'; applying that planted a new isolated label literally named '/DW_VCC'."""
    from copper_kicad.tools import sch_tools as ST
    d = ST.compact_erc({"errors": 1, "warnings": 0, "by_type": {"power_pin_not_driven": 1},
                        "top": [{"severity": "error", "type": "power_pin_not_driven",
                                 "message": "Input Power pin not driven - U1.3 (net /power/DW_VCC)"}]})
    assert '"net":"DW_VCC"' in d["issues"][0]["fix"] and "/DW_VCC" not in d["issues"][0]["fix"], d
    pr = _project("slashnet")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Regulator_Linear:AMS1117-3.3", "ref": "U1", "at": [100.33, 60.96]},
                     {"op": "place", "lib_id": "Device:R", "ref": "R1", "value": "1k", "at": [60.96, 60.96]},
                     {"op": "connect", "pins": ["U1.VI", "R1.1"], "net": "DW_VCC"},
                     {"op": "connect", "pins": ["U1.GND", "R1.2"], "net": "GND"},
                     {"op": "connect", "pin": "U1.VO", "net": "+3V3"}])
    assert any(v["type"] == "power_pin_not_driven" for v in _wiring_errors(pr))
    r = O.apply_ops(pr, [{"op": "pwr_flag", "net": "/DW_VCC"}, {"op": "pwr_flag", "net": "GND"}])
    assert any("sheet-path form" in w for w in r["warnings"]), r["warnings"]
    sch = Schematic.load(pr.sch)
    assert not [l for l in sch.labels() if l["net"].startswith("/")]
    assert not _wiring_errors(pr), [(v["type"], v["description"]) for v in _wiring_errors(pr)]
    # a no_connect on a PWR_FLAG pin is meaningless (an old ERC hint suggested it)
    flag = next(s.ref for s in sch.symbols() if O.is_flag(s))
    assert _code(lambda: O.apply_ops(pr, [{"op": "no_connect", "pin": f"{flag}.1"}], dry_run=True)) == "BAD_OP"


def test_delete_removes_wires_it_leaves_dangling():
    """Deleting a hand-wired part / a PWR_FLAG used to leave wires no op could remove."""
    pr = _demo("pic_programmer", "pic_del")
    sub = "pic_sockets"
    r = O.apply_ops(pr, [{"op": "delete", "ref": "U1"}], sheet=sub)
    assert "wire segment" in r["changed"][0] and not r["warnings"], (r["changed"], r["warnings"])
    bad = [v["type"] for v in _erc(pr) if v["type"] in ("wire_dangling", "unconnected_wire_endpoint", "label_dangling")]
    assert not bad, bad
    # delete a PWR_FLAG by reference: its wire / label go with it
    pr2 = _project("del_flag")
    O.apply_ops(pr2, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [50.8, 50.8]},
                      {"op": "connect", "pin": "R1.1", "net": "SIG"}, {"op": "connect", "pin": "R1.2", "net": "GND"},
                      {"op": "pwr_flag", "net": "SIG"}])
    flag = next(s.ref for s in Schematic.load(pr2.sch).symbols() if O.is_flag(s))
    n_wires = len(Schematic.load(pr2.sch).wires())
    r = O.apply_ops(pr2, [{"op": "delete", "ref": flag}])
    sch = Schematic.load(pr2.sch)
    assert len(sch.wires()) < n_wires and sum(1 for l in sch.labels() if l["net"] == "SIG") == 1
    # explicit stray-wire removal: wire_at, then dangling:true
    O.apply_ops(pr2, [{"op": "wire", "points": [[30.48, 30.48], [30.48, 40.64]]}])
    r = O.apply_ops(pr2, [{"op": "delete", "dangling": True}])
    assert "removed 1 dangling" in r["changed"][0], r["changed"]
    O.apply_ops(pr2, [{"op": "wire", "points": [[30.48, 30.48], [30.48, 40.64]]}])
    r = O.apply_ops(pr2, [{"op": "delete", "wire_at": [30.48, 35.56]}])
    assert "deleted 1 wire segment" in r["changed"][0]
    assert _code(lambda: O.apply_ops(pr2, [{"op": "delete", "wire_at": [30.48, 35.56]}])) == "NOT_FOUND"


def test_delete_label_kind_filter_and_rename_net():
    pr = _demo("pic_programmer", "pic_lbl")
    r = O.apply_ops(pr, [{"op": "delete", "label": "VPP-MCLR", "kind": "local"}], sheet="pic_sockets", dry_run=True)
    assert r["changed"][0].startswith("deleted 4 label(s)") and not r["warnings"], r
    r = O.apply_ops(pr, [{"op": "delete", "label": "VPP-MCLR"}], sheet="pic_sockets", dry_run=True)
    assert "1 hierarchical" in r["changed"][0] and any("parent sheet's pin" in w for w in r["warnings"]), r
    r = O.apply_ops(pr, [{"op": "rename_net", "from": "CLOCK-RB6", "to": "CLK_RB6"}], sheet="pic_sockets", dry_run=True)
    assert "renamed net CLOCK-RB6 → CLK_RB6 (6 label(s))" in r["changed"][0] and len(r["warnings"]) == 1, r
    assert _code(lambda: O.apply_ops(pr, [{"op": "rename_net", "from": "CLOCK-RB6", "to": "DATA-RB7"}],
                                     sheet="pic_sockets", dry_run=True)) == "NET_CONFLICT"
    # joining an existing hierarchical net uses a local label (no extra sheet-interface label)
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "id": "r"}, {"op": "connect", "pin": "r.1", "net": "VCC_PIC"},
                     {"op": "connect", "pin": "r.2", "net": "VPP-MCLR"}], sheet="pic_sockets")
    sch = Schematic.load(pr.dir / "pic_sockets.kicad_sch")
    assert sum(1 for l in sch.labels() if l["kind"] == "hierarchical") == 4
    pn = N.pin_nets(N.export_netlist(pr))
    new_r = [s.ref for s in sch.symbols(include_power=False) if s.lib_id == "Device:R"][0]
    assert pn[(new_r, "1")] == pn[("U5", "14")]          # VDD of the PIC socket = VCC_PIC


def test_multi_instance_refs_numbering_and_chains():
    """complex_hierarchy: refs of the 2nd instance, per-sheet numbering (R214/R314), R→LED chain stays wired."""
    pr = _demo("complex_hierarchy", "cx_refs")
    try:
        O.apply_ops(pr, [{"op": "set_field", "ref": "R302", "fields": {"LCSC": "C25819"}}], dry_run=True)
        assert False
    except ToolError as e:
        assert e.code == "REF_NOT_FOUND" and "sheet='ampli_ht_horizontal'" in (e.fix or ""), e.fix
    r = O.apply_ops(pr, [{"op": "set_field", "ref": "R302", "fields": {"LCSC": "C25819"}}], sheet="ampli_ht.kicad_sch")
    assert "ampli_ht_horizontal" in r["changed"][0]
    assert N.export_netlist(pr)["components"]["R302"]["fields"].get("LCSC") == "C25819"
    r = O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "value": "2.2k", "id": "r", "near": "U201", "footprint": R06},
                         {"op": "place", "lib_id": "Device:LED", "id": "led"},
                         {"op": "connect", "pin": "r.1", "net": "VCC"}, {"op": "connect", "pin": "led.K", "net": "GND"},
                         {"op": "connect", "pins": ["r.2", "led.A"], "net": "LED_A"}], sheet="ampli_ht.kicad_sch")
    assert r["refs"] == {"r": "R214", "led": "D205"}, r["refs"]
    assert any("R314" in c for c in r["changed"]), r["changed"]
    assert any("wired as LED_A" in c for c in r["changed"]), r["changed"]
    assert not N.check_expectations(N.export_netlist(pr), r["expect"])["mismatches"]


def test_dogfood_place_near_small_part_and_chain():
    """place with near=<id of another small part> crashed with KeyError in _do_places; the chain must still stack."""
    pr = _project("near_small")
    r = O.apply_ops(pr, [{"op": "place", "lib_id": "MCU_Microchip_ATtiny:ATtiny85-20S", "id": "mcu"},
                         {"op": "place", "lib_id": "Device:LED", "id": "led", "near": "mcu"},
                         {"op": "place", "lib_id": "Device:R", "value": "330", "id": "rl", "near": "led"},
                         {"op": "connect", "pins": ["mcu.PB0", "rl.1"], "net": "LED_DRV"},
                         {"op": "connect", "pins": ["rl.2", "led.A"], "net": "LED_A"},
                         {"op": "connect", "pins": ["led.K", "mcu.GND"], "net": "GND"},
                         {"op": "connect", "pin": "mcu.VCC", "net": "+5V"}])
    assert any("wired as LED_A" in c for c in r["changed"]), r["changed"]
    sch = Schematic.load(pr.sch)
    rl, led = sch.find_ref(r["refs"]["rl"])[0], sch.find_ref(r["refs"]["led"])[0]
    assert abs(rl.at[0] - led.at[0]) < 0.01, (rl.at, led.at)      # stacked in one column
    assert not N.check_expectations(N.export_netlist(pr), r["expect"])["mismatches"]


def test_dogfood_swap_then_rename_in_one_batch():
    """swap + set_field Reference in one batch used to report false connectivity mismatches."""
    pr = _base("swap_rename")
    r = O.apply_ops(pr, [{"op": "swap", "ref": "R1", "lib_id": "Device:R_Small", "footprint": R06},
                         {"op": "set_field", "ref": "R1", "field": "Reference", "value": "R7"},
                         {"op": "connect", "pin": "R7.2", "net": "IN"}])
    chk = N.check_expectations(N.export_netlist(pr), r["expect"])
    assert chk["checked"] and not chk["mismatches"], chk


def test_dogfood_pin_synonyms():
    ldo = [{"number": "1", "name": "GND", "alternates": []}, {"number": "2", "name": "VO", "alternates": []},
           {"number": "3", "name": "VI", "alternates": []}, {"number": "4", "name": "EN", "alternates": []}]
    assert [p["number"] for p in G.match_pins(ldo, "VIN")[0]] == ["3"]
    assert [p["number"] for p in G.match_pins(ldo, "IN")[0]] == ["3"]
    assert [p["number"] for p in G.match_pins(ldo, "VOUT")[0]] == ["2"]
    assert [p["number"] for p in G.match_pins(ldo, "OUT")[0]] == ["2"]
    assert G.match_pins(ldo, "VSS") == ([ldo[0]], "synonym")
    assert [p["number"] for p in G.match_pins(ldo, "ENABLE")[0]] == ["4"]
    # only when exactly ONE pin name of the group matches
    two = ldo + [{"number": "5", "name": "OUT", "alternates": []}]
    assert G.match_pins(two, "VOUT") == ([], "")


def test_power_rails_never_run_past_another_pin():
    """A shared power rail on one edge must not pass over a pin of another net (it shorted CC1 to VBUS in tidy)."""
    pr = _project("rails")
    r = O.apply_ops(pr, [{"op": "place", "lib_id": "Connector_Generic:Conn_01x03", "ref": "J1", "at": [50.8, 50.8]},
                         {"op": "place", "lib_id": "Connector_Generic:Conn_01x02", "ref": "J2", "at": [101.6, 50.8]},
                         {"op": "connect", "pins": ["J1.1", "J1.3"], "net": "GND"},
                         {"op": "connect", "pin": "J1.2", "net": "SIG"},
                         {"op": "connect", "pins": ["J2.1", "J2.2"], "net": "GND"}])
    assert any("shared rail" in c and "J2" in c for c in r["changed"]), r["changed"]
    assert not any("shared rail" in c and "J1" in c for c in r["changed"]), r["changed"]
    pn = N.pin_nets(N.export_netlist(pr))
    assert N.short_name(pn[("J1", "2")]) == "SIG" and pn[("J1", "1")] == pn[("J1", "3")] == pn[("J2", "2")]
    sch = Schematic.load(pr.sch)
    assert sum(1 for s in sch.symbols() if s.is_power and s.value == "GND") == 3   # 2 on J1, 1 shared on J2


def test_lcsc_value_units_and_connector_prefix(monkeypatch=None):
    """swap/place from LCSC wrote '27' for a 27R resistor; an EasyEDA pin-header symbol came in as U4."""
    assert O.si_value("27R") == O.si_value("27") == 27 and O.si_value("4k7") == 4700
    assert O._value_with_unit("27", "R") == "27R" and O._value_with_unit("100nF", "C") == "100nF"
    orig = O._from_lcsc
    parts = {"C25100": {"lib_id": "Device:R", "footprint": R06, "value": "27", "fields": {"LCSC": "C25100"}},
             "C5383109": {"lib_id": "Regulator_Linear:AMS1117-3.3",
                          "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
                          "value": "HDR-1x3", "fields": {"LCSC": "C5383109", "Description": "2.54mm pin header"}}}
    O._from_lcsc = lambda ctx, lcsc: dict(parts[lcsc], fields=dict(parts[lcsc]["fields"]))
    try:
        pr = _project("lcsc_units")
        r = O.apply_ops(pr, [{"op": "place", "lcsc": "C25100", "id": "r", "at": [50.8, 50.8]},
                             {"op": "place", "lcsc": "C5383109", "id": "h", "at": [101.6, 50.8]},
                             {"op": "place", "lib_id": "Device:R", "ref": "R9", "value": "27R", "at": [76.2, 50.8]}])
        assert r["refs"]["h"].startswith("J"), r["refs"]
        sch = Schematic.load(pr.sch)
        assert sch.find_ref(r["refs"]["r"])[0].value == "27R"
        O.apply_ops(pr, [{"op": "swap", "ref": "R9", "lcsc": "C25100"}])
        assert Schematic.load(pr.sch).find_ref("R9")[0].value == "27R"
    finally:
        O._from_lcsc = orig


def _clutter(sch: Schematic) -> list[tuple]:
    """(power symbol, label) pairs whose drawings overlap or sit closer than 0.2 mm."""
    bad = []
    labels = [(l["net"], LAY.label_box(l)) for l in sch.labels()]
    for s in sch.symbols():
        if not s.is_power:
            continue
        for b in [s.body_bbox()] + s.field_boxes():
            bad += [(f"{s.ref} {s.value}", n) for n, lb in labels if LAY._rect_hit(b, lb, 0.2)]
    return bad


def test_text_width_matches_kicad_stroke_font():
    """The old estimate was ~20 % short (0.78 × size per capital; KiCad draws ~0.95), so label room was
    under-reserved and labels of stacked parts met end to end ('BOOTSEL USB_DP'). Measured on kicad-cli plots:
    'BOOTSEL' ≈ 7.9 mm, 'USB_DP' ≈ 6.8 mm at 1.27 mm text."""
    from copper_kicad.sch.model import text_width
    assert 7.6 < text_width("BOOTSEL") < 8.3, text_width("BOOTSEL")
    assert 6.5 < text_width("USB_DP") < 7.2, text_width("USB_DP")
    assert text_width("~{RESET}") == text_width("RESET")
    # a vertical label's box runs from its anchor over the whole text, left of the wire
    b = LAY.label_box({"net": "BOOTSEL", "kind": "local", "at": [100.0, 50.0], "rot": 90})
    assert b[3] == 50.0 and 50.0 - b[1] > 8.0 and b[2] < 100.0, b
    # stacked two-pin parts with facing vertical labels keep a readable gap between the two texts
    pr = _project("endtoend")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "id": "a", "value": "27"},
                     {"op": "place", "lib_id": "Device:R", "id": "b", "value": "1k"},
                     {"op": "place", "lib_id": "Device:R", "id": "c", "value": "1k"},
                     {"op": "connect", "pin": "a.1", "net": "USB_CONN_DP"}, {"op": "connect", "pin": "a.2", "net": "USB_DP"},
                     {"op": "connect", "pin": "b.1", "net": "BOOTSEL"}, {"op": "connect", "pin": "b.2", "net": "QSPI_SS"},
                     {"op": "connect", "pin": "c.1", "net": "USB_CONN_DN"}, {"op": "connect", "pin": "c.2", "net": "USB_DM"}])
    boxes = [(l["net"], LAY.label_box(l)) for l in Schematic.load(pr.sch).labels()]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert not LAY._rect_hit(boxes[i][1], boxes[j][1], 1.0), (boxes[i], boxes[j])


def test_power_symbols_clear_neighbouring_labels():
    """A GND / +3V3 symbol on a side pin sat on the label of the pin below it (GND triangle over 'NRST',
    '+3V3' run into 'SWDIO'). The symbol now moves out along its own stub; the netlist is unchanged."""
    pr = _project("declutter")
    r = O.apply_ops(pr, [
        {"op": "place", "lib_id": "Connector_Generic:Conn_01x05", "ref": "J1"},
        {"op": "place", "lib_id": "Connector_Generic:Conn_02x05_Odd_Even", "ref": "J2"},
        {"op": "connect", "pin": "J1.1", "net": "+3V3"}, {"op": "connect", "pin": "J1.2", "net": "SWDIO"},
        {"op": "connect", "pin": "J1.3", "net": "SWCLK"}, {"op": "connect", "pin": "J1.4", "net": "GND"},
        {"op": "connect", "pin": "J2.1", "net": "+3V3"}, {"op": "connect", "pin": "J2.3", "net": "GND"},
        {"op": "connect", "pin": "J2.5", "net": "GPIO0"}, {"op": "connect", "pin": "J2.7", "net": "+5V"},
        {"op": "connect", "pin": "J2.9", "net": "GPIO14"}, {"op": "connect", "pin": "J2.2", "net": "VBUS"},
        {"op": "connect", "pin": "J2.4", "net": "GND"}, {"op": "connect", "pin": "J2.6", "net": "GPIO1"},
        {"op": "connect", "pin": "J2.8", "net": "GND"}, {"op": "connect", "pin": "J2.10", "net": "GND"}])
    assert any("further out" in c for c in r["changed"]), r["changed"]
    sch = Schematic.load(pr.sch)
    assert not _clutter(sch), _clutter(sch)
    want = {("J1", "1"): "+3V3", ("J1", "2"): "/SWDIO", ("J1", "4"): "GND", ("J2", "3"): "GND", ("J2", "7"): "+5V",
            ("J2", "2"): "VBUS", ("J2", "4"): "GND", ("J2", "6"): "/GPIO1", ("J2", "8"): "GND", ("J2", "10"): "GND"}
    pn = N.pin_nets(N.export_netlist(pr))
    assert all(pn.get(k) == v for k, v in want.items()), {k: pn.get(k) for k in want}
    assert N.check_expectations(N.export_netlist(pr), r["expect"])["mismatches"] == []
    odd = [v for v in _wiring_errors(pr) if v["type"] not in ("power_pin_not_driven", "pin_not_connected")]
    assert not odd, odd                               # only the expected 'no source on this test sheet' findings
    # a label added LATER next to an existing power symbol moves that symbol too (and only then)
    before = {s.ref: s.at for s in sch.symbols() if s.is_power}
    r2 = O.apply_ops(pr, [{"op": "connect", "pin": "J1.5", "net": "NRST"}])
    sch = Schematic.load(pr.sch)
    after = {s.ref: s.at for s in sch.symbols() if s.is_power}
    moved = [k for k in before if before[k] != after[k]]
    assert len(moved) == 1 and any("further out" in c for c in r2["changed"]), (moved, r2["changed"])
    assert not _clutter(sch), _clutter(sch)
    pn2 = N.pin_nets(N.export_netlist(pr))
    assert pn2[("J1", "5")] == "/NRST" and all(pn2.get(k) == v for k, v in want.items()), pn2
    # nothing to do → nothing moves, nothing reported
    r3 = O.apply_ops(pr, [{"op": "place", "lib_id": "Device:R", "ref": "R1", "value": "1k"}])
    assert not any("further out" in c for c in r3["changed"])
    assert {s.ref: s.at for s in Schematic.load(pr.sch).symbols() if s.is_power} == after


def test_declutter_never_extends_onto_other_copper():
    """The stub is only lengthened over empty sheet: a wire, pin or part in the way leaves the symbol where it is."""
    pr = _project("declutter_blocked")
    O.apply_ops(pr, [{"op": "place", "lib_id": "Connector_Generic:Conn_01x03", "ref": "J1", "at": [101.6, 50.8]},
                     {"op": "connect", "pin": "J1.1", "net": "GND"}])
    sch = Schematic.load(pr.sch)
    g = next(s for s in sch.symbols() if s.is_power)
    tip = next(p["xy"] for p in sch.find_ref("J1")[0].pins_world() if p["number"] == "1")
    out = (-1, 0) if g.at[0] < tip[0] else (1, 0)
    x = g.at[0] + out[0] * 5.08                       # a foreign vertical wire right across the escape path
    O.apply_ops(pr, [{"op": "wire", "points": [[x, tip[1] - 5.08], [x, tip[1] + 7.62]]},
                     {"op": "label", "net": "OTHER", "at": [x, tip[1] - 5.08]}])
    at0 = next(s for s in Schematic.load(pr.sch).symbols() if s.is_power).at
    r = O.apply_ops(pr, [{"op": "connect", "pin": "J1.2", "net": "SIGNAL_NAME"}])
    assert next(s for s in Schematic.load(pr.sch).symbols() if s.is_power).at == at0
    pn = N.pin_nets(N.export_netlist(pr))
    assert pn[("J1", "1")] == "GND" and pn[("J1", "2")] == "/SIGNAL_NAME", pn
    assert not any(n["ref"] == "J1" for n in N.export_netlist(pr)["nets"].get("/OTHER", []))


def test_erc_examples_keep_position_and_sheet_routed_fix():
    """sch_apply/sch_tidy used to drop each finding's `at` and take the first finding's fix for the whole type, so a
    problem on a sub-sheet came back with a root-sheet fix."""
    from copper_kicad.tools import sch_tools as ST
    fx = lambda pin, sh: f"sch_apply({sh}ops=[{{\"op\":\"no_connect\",\"pin\":\"{pin}\"}}])"   # noqa: E731
    raw = {"errors": 3, "warnings": 2, "new": 3, "fixed": 0, "by_type": {"pin_not_connected": 2, "power_pin_not_driven": 2,
                                                                         "wire_dangling": 1},
           "top": [{"severity": "error", "type": "pin_not_connected", "message": "Pin not connected — U1.3", "at": [10.0, 20.0],
                    "fix": fx("U1.3", ""), "sheet": "/"},
                   {"severity": "error", "type": "pin_not_connected", "message": "Pin not connected — U7.2", "at": [30.0, 40.0],
                    "fix": fx("U7.2", "sheet='power', "), "sheet": "/power/"},
                   {"severity": "error", "type": "power_pin_not_driven", "message": "x — U1.8 (net VBUS)", "at": [1.0, 2.0],
                    "fix": "?", "sheet": "/"},
                   {"severity": "error", "type": "power_pin_not_driven", "message": "x — U7.5 (net /power/VBAT)",
                    "at": [3.0, 4.0], "fix": "?", "sheet": "/power/"},
                   {"severity": "warning", "type": "wire_dangling", "message": "dangling", "at": [5.0, 6.0], "fix": "F"}]}
    d = ST.compact_erc(raw)
    by = {g["type"]: g for g in d["issues"]}
    ex = by["pin_not_connected"]["examples"]
    assert [e["at"] for e in ex] == [[10.0, 20.0], [30.0, 40.0]] and "sheet" not in ex[0] and ex[1]["sheet"] == "power"
    assert "sheet='power'" in ex[1]["fix"] and "U7.2" in ex[1]["fix"] and "sheet=" not in ex[0]["fix"]
    assert "fix" not in by["pin_not_connected"]                 # different remedies are not collapsed into one
    pf = by["power_pin_not_driven"]
    assert 'sch_apply(ops=[{"op":"pwr_flag","net":"VBUS"}])' in pf["fix"], pf["fix"]
    assert 'sch_apply(sheet="power", ops=[{"op":"pwr_flag","net":"VBAT"}])' in pf["fix"], pf["fix"]
    assert [e["at"] for e in pf["examples"]] == [[1.0, 2.0], [3.0, 4.0]]
    one = by["wire_dangling"]
    assert one["message"] == "dangling" and one["at"] == [5.0, 6.0] and one["fix"] == "F"
    # end to end: the real ERC of a real sheet carries positions through the tool
    from copper_kicad.mcp import registry
    import copper_kicad.tools  # noqa: F401
    pr = _base("erc_at")
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "preview": False,
                                           "ops": [{"op": "place", "lib_id": "Device:R", "ref": "R7", "value": "1k"}]})
    assert not out["isError"], out
    issues = out["structuredContent"]["verify"]["erc"].get("issues") or []
    flat = [e for g in issues for e in (g.get("examples") or [g])]
    assert flat and all(isinstance(e.get("at"), list) and len(e["at"]) == 2 for e in flat), issues
    assert any("R7" in (e.get("fix") or g.get("fix") or "") for g in issues for e in (g.get("examples") or [g])), issues


def test_sch_read_uses_kicad_net_names_and_says_what_it_excludes():
    """sch_read said 'IN' where project_summary / the board say '/IN', and silently left nets out."""
    from copper_kicad.mcp import registry
    import copper_kicad.tools  # noqa: F401
    pr = _base("read_nets")

    def read(**kw):
        out = registry.call_tool("sch_read", {"project": str(pr.pro), **kw})
        assert not out["isError"], out
        return out["structuredContent"]
    nl = N.export_netlist(pr)
    d = read(query="nets")
    assert set(d["nets"]) == {n for n in nl["nets"] if not N.is_unconnected(n)} and "/IN" in d["nets"] and "+5V" in d["nets"]
    unconn = sum(1 for n in nl["nets"] if N.is_unconnected(n))
    assert unconn and d["all_nets"] == len(nl["nets"]) and d["total"] == len(nl["nets"]) - unconn
    assert any(str(unconn) in e and "unconnected" in e for e in d["excluded"]), d["excluded"]
    assert "label name" in d["names"]
    assert list(read(query="nets", net="IN")["nets"]) == ["/IN"] == list(read(query="nets", net="/IN")["nets"])
    f = read(query="nets", filter="5v")
    assert list(f["nets"]) == ["+5V"] and any("not matching" in e for e in f["excluded"]), f
    lim = read(query="nets", limit=1)
    assert len(lim["nets"]) == 1 and any("beyond limit" in e for e in lim["excluded"]), lim
    pins = read(query="pins", ref="U1", filter="3")
    assert pins["pins"][0]["net"] == "/IN", pins
    sm = read(query="summary")
    assert sm["nets"] == len(nl["nets"]) and sm["unconnected_pin_nets"] == unconn
    assert sm["connected_nets"] == sum(1 for v in nl["nets"].values() if len(v) >= 2)


def test_spec_placement_reports_pick_warning_and_class():
    """A spec that resolved to an Extended part said nothing; one with no stocked match blamed a 'parts engine
    unavailable' that was in fact running."""
    from copper_kicad.parts import basic
    orig_pick, orig_lcsc = basic.pick, O._from_lcsc
    O._from_lcsc = lambda ctx, lcsc: {"lib_id": "Device:LED", "footprint": "LED_SMD:LED_0603_1608Metric", "value": "LED",
                                      "fields": {"LCSC": lcsc, "JLC Class": "Extended"}}
    try:
        basic.pick = lambda spec, *a, **k: {"lcsc": "C72043", "value": "LED green", "class": "Extended",
                                            "warning": "C72043 is JLC Extended (no Basic part matches; Basic C2297 is 0805)"}
        pr = _project("spec_warn")
        r = O.apply_ops(pr, [{"op": "place", "spec": "LED green 0603", "id": "d"}])
        assert any("LED green 0603" in w and "Basic C2297 is 0805" in w for w in r["warnings"]), r["warnings"]
        placed = next(c for c in r["changed"] if c.startswith("placed"))
        assert "LCSC C72043" in placed and "(JLC Extended)" in placed, placed
        basic.pick = lambda spec, *a, **k: None
        r = O.apply_ops(pr, [{"op": "place", "spec": "27R 0402", "id": "r"}])
        w = next(w for w in r["warnings"] if "27R 0402" in w)
        assert "no in-stock LCSC/JLC part matches" in w and "unavailable" not in w and "parts_search" in w, w
        assert "JLC" not in next(c for c in r["changed"] if c.startswith("placed"))
    finally:
        basic.pick, O._from_lcsc = orig_pick, orig_lcsc


def _run_all():
    import time
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
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
    print(f"\n{len(tests) - failed}/{len(tests)} passed — artifacts in {TMP}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
