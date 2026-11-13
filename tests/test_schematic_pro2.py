"""Regression tests from the skeptical test pass over the professional schematic layout (sch/pro).

    python tests/test_schematic_pro2.py            (plain runner, real KiCad 10 install)

Each test pins a bug that was found by using the feature the way an AI client does, on boards the builder did not
use: the ERC half of the identity gate compared the candidate with itself, a multi-unit part made every second
re-layout fail, a ring of series parts recursed for ever, sch_apply never re-laid a sheet after the first batch,
a second sch_tidy re-wrote the file, user notes ended up on top of the new drawing, decoupling caps left their IC.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-home-"))

from copper_kicad import sexpr  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.model import Schematic  # noqa: E402
from copper_kicad.sch.netlist import export_netlist  # noqa: E402
from copper_kicad.sch.pro import emit as E  # noqa: E402
from copper_kicad.sch.pro import engine, gate  # noqa: E402
from copper_kicad.sch.pro import tool as SP  # noqa: E402
from copper_kicad.sch.pro.blocks import Design  # noqa: E402
import copper_kicad.tools  # noqa: E402,F401  (registers the tools)

TMP = Path(tempfile.mkdtemp(prefix="copper-pro2-"))


def _demo(dname: str, tag: str) -> Project:
    d = TMP / tag
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(env.kicad().share / "demos" / dname, d,
                    ignore=shutil.ignore_patterns("*.pretty", "*.kicad_pcb", "*.step", "*.wrl", "*.pdf", "*.zip"))
    return Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)


def _new(tag: str) -> Project:
    d = TMP / tag
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return Project(d, tag)


def _apply(pr, ops, **kw):
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "ops": ops, "preview": False, **kw})
    assert not out.get("isError"), out["content"][0]["text"][:600]
    return out["structuredContent"]


def _tidy(pr, **kw):
    out = registry.call_tool("sch_tidy", {"project": str(pr.pro), "preview": False, **kw})
    assert not out.get("isError"), out["content"][0]["text"][:600]
    return out["structuredContent"]


def _erc_types(root: Path) -> dict:
    """{violation type: count} straight from kicad-cli (the independent witness)."""
    import json
    out = root.parent / "_erc_witness.json"
    env.run(["sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(root)], timeout=300)
    d = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    n: dict = {}
    for sh in d.get("sheets", []):
        for v in sh.get("violations", []):
            n[v["type"]] = n.get(v["type"], 0) + 1
    return n


AMP = [{"op": "place", "lib_id": "Regulator_Linear:L7805", "id": "reg"},
       {"op": "place", "lib_id": "Device:C", "id": "cin", "value": "330n", "near": "reg"},
       {"op": "place", "lib_id": "Device:C", "id": "cout", "value": "100n", "near": "reg"},
       {"op": "place", "lib_id": "Amplifier_Operational:LM358", "id": "amp"},
       {"op": "place", "lib_id": "Device:R", "id": "rf", "value": "100k", "near": "amp"},
       {"op": "place", "lib_id": "Device:R", "id": "rg", "value": "10k", "near": "amp"},
       {"op": "place", "lib_id": "Connector_Generic:Conn_01x04", "id": "j"},
       {"op": "connect", "pins": ["reg.VI", "cin.1", "j.1"], "net": "+12V"},
       {"op": "connect", "pins": ["reg.GND", "cin.2", "cout.2", "j.4", "rg.2", "amp.4", "amp.5"], "net": "GND"},
       {"op": "connect", "pins": ["reg.VO", "cout.1", "amp.8"], "net": "+5V"},
       {"op": "connect", "pins": ["amp.3", "j.2"], "net": "SIG_IN"},
       {"op": "connect", "pins": ["amp.2", "rf.1", "rg.1"], "net": "FB"},
       {"op": "connect", "pins": ["amp.1", "rf.2", "j.3"], "net": "SIG_OUT"},
       {"op": "connect", "pins": ["amp.6", "amp.7"], "net": "BUF"},
       {"op": "pwr_flag", "net": "+12V"}, {"op": "pwr_flag", "net": "GND"}]


# ───────────────────────── the gate ─────────────────────────
def test_g4_compares_with_the_original_sheet():
    """relayout() takes its baseline without ERC and asks for it lazily. The lazy ERC used to run while the staged
    file already held the candidate, so 'nothing new' was the candidate compared with itself: forcing a re-layout of
    tiny_tapeout/rp2040 wrote a sheet with a NEW 'Both +3V3 and IOVDD are attached' ERC error."""
    pr = _demo("tiny_tapeout", "g4")
    before = _erc_types(pr.sch)
    f, sp = O.sheet_file(pr, "rp2040.kicad_sch")
    raw = f.read_bytes()
    try:
        r = engine.relayout(pr, "rp2040.kicad_sch", force=True)
        wrote = bool(r.get("revision"))
    except ToolError as e:
        assert e.code == "LAYOUT_REFUSED", e.code
        wrote = False
    if wrote:
        after = _erc_types(pr.sch)
        assert after.get("multiple_net_names", 0) <= before.get("multiple_net_names", 0), (before, after)
    else:
        assert f.read_bytes() == raw
    # unit level: a baseline without ERC must end up with the ERC of the ORIGINAL, whatever candidate is checked
    pr2 = _demo("tiny_tapeout", "g4b")
    f2, sp2 = O.sheet_file(pr2, "rp2040.kicad_sch")
    b_ref = gate.baseline(pr2, f2, sp2, with_erc=True)
    b = gate.baseline(pr2, f2, sp2, with_erc=False)
    try:
        assert b.erc is None
        gate.check(pr2, b, f2.read_text(encoding="utf-8"), sp2)
        assert b.erc == b_ref.erc
        assert b.sheet.read_bytes() == b_ref.sheet.read_bytes()        # staged tree restored
    finally:
        b.close()
        b_ref.close()


def test_canon_ignores_unit_order_of_multi_unit_parts():
    """(tstamps a b c) lists the units of one part in the order KiCad met them, which follows their position."""
    def tree(order):
        return sexpr.loads('(export (components (comp (ref "U3") (value "LM358") (tstamps %s))) (libparts) '
                           '(nets (net (code "1") (name "GND") (class "Default") (node (ref "U3") (pin "4")))))'
                           % " ".join(f'"{u}"' for u in order))
    a, b = gate.canon(tree(["u1", "u2", "u3"])), gate.canon(tree(["u2", "u1", "u3"]))
    assert not gate.canon_diff(a, b) and gate.canon_hash(a) == gate.canon_hash(b)
    c = gate.canon(tree(["u1", "u2", "u9"]))
    assert gate.canon_diff(a, c)                                        # a different unit is still a difference


# ───────────────────────── sch_apply: every batch, not only the first ─────────────────────────
def test_second_batch_is_relaid_and_tidy_is_a_noop():
    pr = _new("incr")
    d1 = _apply(pr, AMP)
    assert d1.get("layout", "").startswith("re-laid"), d1.get("layout")
    s = Schematic.load(pr.sch, pr.name)
    assert E.is_engine_layout(s)
    # a second position-free batch on an untouched engine sheet (multi-unit LM358 on board) is re-laid again
    d2 = _apply(pr, [{"op": "place", "lib_id": "Device:R", "id": "x", "value": "4k7"},
                     {"op": "connect", "pin": "x.1", "net": "+5V"}, {"op": "connect", "pin": "x.2", "net": "SIG_IN"}])
    assert d2.get("layout", "").startswith("re-laid"), d2.get("layout")
    assert E.is_engine_layout(Schematic.load(pr.sch, pr.name))
    # `wire` between two pins names no position either
    d3 = _apply(pr, [{"op": "place", "lib_id": "Device:R", "id": "r", "value": "1k"},
                     {"op": "place", "lib_id": "Device:LED", "id": "d"},
                     {"op": "connect", "pin": "r.1", "net": "+5V"}, {"op": "wire", "from": "r.2", "to": "d.A"},
                     {"op": "connect", "pin": "d.K", "net": "GND"}])
    assert d3.get("layout", "").startswith("re-laid"), d3.get("layout")
    s = Schematic.load(pr.sch, pr.name)
    assert E.is_engine_layout(s)
    # the rail chain +5V - R - LED - GND is drawn in line: one wire group holds both parts, a few mm apart
    rr, dd = d3["refs"]["r"], d3["refs"]["d"]
    assert any({rr, dd} <= {p[0] for p in g["pins"]} for g in O.geo_groups(s)), "R and LED are not wired together"
    a, b = s.find_ref(rr)[0].at, s.find_ref(dd)[0].at
    assert abs(a[0] - b[0]) + abs(a[1] - b[1]) < 30.0, (a, b)
    # sch_tidy in the style the sheet was drawn in (sch_apply draws "pro"): nothing to do, not a byte written, no
    # checkpoint taken
    raw = pr.sch.read_bytes()
    snaps = sorted(p.name for p in (pr.dir / ".copper" / "snapshots").iterdir())
    t = _tidy(pr, style="pro")
    assert t["style_used"] == "unchanged", t
    assert pr.sch.read_bytes() == raw
    assert sorted(p.name for p in (pr.dir / ".copper" / "snapshots").iterdir()) == snaps
    # sch_tidy with its own default (pro2) may draw the sheet once more in the newer style; asked again it has
    # nothing to do, and the sheet is still the engine's (the next position-free batch is re-laid)
    t = _tidy(pr)
    assert t["style_used"] in ("pro2", "unchanged") and "fallback" not in t, t
    raw = pr.sch.read_bytes()
    snaps = sorted(p.name for p in (pr.dir / ".copper" / "snapshots").iterdir())
    t = _tidy(pr)
    assert t["style_used"] == "unchanged", t
    assert E.is_engine_layout(Schematic.load(pr.sch, pr.name))
    assert pr.sch.read_bytes() == raw
    assert sorted(p.name for p in (pr.dir / ".copper" / "snapshots").iterdir()) == snaps
    # a hand move switches the automatic re-layout off again
    _apply(pr, [{"op": "move", "ref": rr, "by": [-5.08, 0]}])
    d4 = _apply(pr, [{"op": "place", "lib_id": "Device:C", "id": "y"}, {"op": "connect", "pin": "y.1", "net": "GND"},
                     {"op": "connect", "pin": "y.2", "net": "SIG_IN"}])
    assert d4.get("layout", "").startswith("kept"), d4.get("layout")


def test_wants_layout_rule():
    assert SP.wants_layout([{"op": "place", "lib_id": "Device:R"}])
    assert SP.wants_layout([{"op": "wire", "from": "R1.2", "to": "D1.A"}])
    assert SP.wants_layout([{"op": "label", "net": "X", "pin": "R1.1"}])
    assert not SP.wants_layout([{"op": "wire", "points": [[10, 10], [20, 10]]}])
    assert not SP.wants_layout([{"op": "label", "net": "X", "at": [10, 10]}])
    assert not SP.wants_layout([{"op": "place", "lib_id": "Device:R", "at": [50, 50]}])
    assert not SP.wants_layout([{"op": "place", "lib_id": "Device:R"}, {"op": "move", "ref": "R1", "by": [1, 0]}])
    assert not SP.wants_layout([{"op": "set_field", "ref": "R1", "field": "Value", "value": "1k"}])


# ───────────────────────── robustness ─────────────────────────
def test_series_ring_does_not_recurse_for_ever():
    """openair-max/Battery-Holder has a ring of 2-pin parts: the stem builder followed it until RecursionError and
    the tool answered INTERNAL. It must end as an ordinary answer (re-laid or refused) and leave the file alone."""
    pr = _demo("openair-max", "ring")
    f, _ = O.sheet_file(pr, "Battery-Holder.kicad_sch")
    raw = f.read_bytes()
    t = time.time()
    out = registry.call_tool("sch_tidy", {"project": str(pr.pro), "sheet": "Battery-Holder.kicad_sch", "force": True,
                                          "dry_run": True, "preview": False})
    assert time.time() - t < 120
    txt = out["content"][0]["text"]
    assert "INTERNAL" not in txt and "RecursionError" not in txt, txt[:400]
    assert f.read_bytes() == raw
    # two resistors in parallel between the same two nets, hanging on an IC pin
    pr = _new("par")
    d = _apply(pr, [{"op": "place", "lib_id": "Timer:NE555P", "id": "u"},
                    {"op": "place", "lib_id": "Device:R", "id": "a"}, {"op": "place", "lib_id": "Device:R", "id": "b"},
                    {"op": "place", "lib_id": "Device:C", "id": "c"},
                    {"op": "connect", "pins": ["u.DISCH", "a.1", "b.1"], "net": "N1"},
                    {"op": "connect", "pins": ["u.THRES", "a.2", "b.2", "c.1"], "net": "N2"},
                    {"op": "connect", "pins": ["u.GND", "c.2"], "net": "GND"},
                    {"op": "connect", "pin": "u.VCC", "net": "+5V"}])
    assert d.get("layout", "").startswith(("re-laid", "kept")), d.get("layout")
    assert d["connectivity"]["ok"] == d["connectivity"]["checked"]


def test_engine_bug_costs_one_candidate_not_the_call():
    """An unexpected exception inside a candidate layout is recorded as a refused candidate; the next one is tried."""
    pr = _new("boom")
    _apply(pr, AMP, layout="keep")
    real = engine.layout_pro
    calls = []

    def boom(*a, **k):
        calls.append(k.get("use_router"))
        if k.get("use_router"):
            raise KeyError("synthetic engine bug")
        return real(*a, **k)
    engine.layout_pro = boom
    try:
        r = engine.relayout(pr, force=True)
    finally:
        engine.layout_pro = real
    assert r["style_used"] == "pro-plain" and calls[:2] == [True, False], (r["style_used"], calls)
    assert any("engine error" in str(t.get("refused")) for t in r.get("tried", [])), r.get("tried")


# ───────────────────────── what the user drew or said ─────────────────────────
def test_user_notes_are_parked_and_titles_stay_one_line():
    pr = _new("notes")
    _apply(pr, AMP)
    _apply(pr, [{"op": "text", "text": "REVIEW: check R1 before fab", "at": [60, 60]}], verify=False)
    t = _tidy(pr)
    assert t["style_used"] == "kept" and t.get("orphan_graphics") == 1, t            # a note = the user's drawing
    reg = next(b.id for b in Design(Schematic.load(pr.sch, pr.name), export_netlist(pr)).blocks if b.kind == "power")
    t = _tidy(pr, force=True, blocks=[{"id": reg, "title": "5 V\nsupply", "note": "max 1 A"}])
    assert t["style_used"] == "pro2" and "fallback" not in t, t                       # the tool's default style
    assert "1 text note" in t.get("your_notes", ""), t
    s = Schematic.load(pr.sch, pr.name)
    D = Design(s, export_netlist(pr))
    frames = D._frames()
    assert frames
    note = next(n for n in s.items("text") if "REVIEW" in str(n[1]))
    at = sexpr.find(note, "at")
    x, y = float(at[1]), float(at[2])
    assert not any(fb[0] <= x <= fb[2] and fb[1] <= y <= fb[3] for fb in frames.values()), (x, y, frames)
    assert y > max(fb[3] for fb in frames.values()) - 0.01                           # under the blocks
    titles = [str(n[1]) for n in s.items("text")]
    assert "5 V supply" in titles and not any("\n" in x or "\\n" in x for x in titles if "supply" in x), titles
    # and again: the note stays where it was parked, nothing is re-written
    raw = pr.sch.read_bytes()
    t = _tidy(pr, force=True, blocks=[{"id": reg, "title": "5 V\nsupply", "note": "max 1 A"}])
    assert t["style_used"] == "unchanged" and pr.sch.read_bytes() == raw, t


def test_decoupling_cap_stays_with_the_ic_it_was_placed_at():
    """sch_apply places a `near` cap beside its IC; the re-layout used to deal rail caps out by size and reference,
    so the cap the agent put at U2 was drawn at U1."""
    pr = _new("decap")
    ops = [{"op": "place", "lib_id": "Timer:NE555P", "id": "u1"}, {"op": "place", "lib_id": "Timer:NE555P", "id": "u2"},
           {"op": "place", "lib_id": "Device:C", "id": "ca", "value": "100n", "near": "u2"},
           {"op": "place", "lib_id": "Device:C", "id": "cb", "value": "100n", "near": "u1"},
           {"op": "place", "lib_id": "Device:C", "id": "cc", "value": "100n", "near": "u1"},
           {"op": "connect", "pins": ["u1.VCC", "u2.VCC", "ca.1", "cb.1", "cc.1"], "net": "+5V"},
           {"op": "connect", "pins": ["u1.GND", "u2.GND", "ca.2", "cb.2", "cc.2"], "net": "GND"},
           {"op": "connect", "pin": "u1.OUT", "net": "OUT_A"}, {"op": "connect", "pin": "u2.OUT", "net": "OUT_B"}]
    d = _apply(pr, ops)
    assert d.get("layout", "").startswith("re-laid"), d.get("layout")
    D = Design(Schematic.load(pr.sch, pr.name), export_netlist(pr))
    refs = d["refs"]
    assert D.block_of[refs["u1"]] != D.block_of[refs["u2"]], D.block_of
    assert D.block_of[refs["ca"]] == D.block_of[refs["u2"]], D.block_of
    assert D.block_of[refs["cb"]] == D.block_of[refs["u1"]] == D.block_of[refs["cc"]], D.block_of
    frames = D._frames()
    for c, u in (("ca", "u2"), ("cb", "u1"), ("cc", "u1")):
        fb = frames[D.block_of[refs[u]]]
        x, y = Schematic.load(pr.sch, pr.name).find_ref(refs[c])[0].at[:2]
        assert fb[0] <= x <= fb[2] and fb[1] <= y <= fb[3], (c, u, x, y, fb)


def _fixture(name: str, tag: str) -> Project:
    d = TMP / tag / name
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(ROOT / "tests" / "data" / "pro" / name, d)
    return Project(d, name)


def test_labels_rung_accepts_an_absolute_sheet_path():
    """sch_apply's implicit re-layout passes the sheet as an absolute path; the last-resort label style used to
    re-resolve that path inside the staged copy and refuse ('not found in the hierarchy')."""
    pr = _fixture("c3-devboard", "abs")
    r = engine.relayout(pr, str(pr.sch), style="labels", force=True, dry_run=True)
    assert r["style_used"] in ("labels", "unchanged"), r


def test_signal_flow_and_power_path():
    """Supply comes in on the left, the MCU sits right of it, the header it talks through is not left of it; the
    reverse-protection FET belongs to the battery connector, which is the power input; the sheet's summary line
    and revision are in the title block; a second run changes nothing."""
    pr = _fixture("stm32-sensor", "flow")
    D = Design(Schematic.load(pr.sch, pr.name), export_netlist(pr))
    kinds = {b.id: b.kind for b in D.blocks}
    assert kinds["J1"] == "input" and kinds["U3"] == "power" and kinds["U1"] == "main", kinds
    # the 5-pin debug header only talks to the MCU: it is drawn in the MCU's block, not in a frame of its own
    assert "J2" not in kinds and D.block_of["J2"] == "U1", (kinds, D.block_of.get("J2"))
    assert D.role["Q1"]["anchor"] == "J1" and D.role["Q1"]["kind"] == "series", D.role["Q1"]
    stages = {b.id: b.stage for b in D.blocks}
    assert stages["J1"] < stages["U3"] < stages["U1"], stages
    titles = {b.id: b.title for b in D.blocks}
    assert titles["J1"].startswith("POWER INPUT") and "B2B" not in titles["J1"], titles   # no order codes in titles
    r = engine.relayout(pr, force=True)
    assert r["style_used"] == "pro", r
    sch = Schematic.load(pr.sch, pr.name)
    fr = Design(sch, export_netlist(pr))._frames()
    assert fr["J1"][2] <= fr["U1"][0] and fr["U3"][2] <= fr["U1"][0], fr          # supply column left of the MCU
    assert fr["J1"][3] <= fr["U3"][1], fr                                        # input above its regulator
    assert abs(fr["J1"][0] - fr["U3"][0]) < 0.01 and abs(fr["J1"][2] - fr["U3"][2]) < 0.01, fr   # one column, one width
    tb = sexpr.find(sch.tree, "title_block")
    c1 = next((str(e[2]) for e in sexpr.find_all(tb, "comment") if int(e[1]) == 1), "")
    assert c1.startswith("Blocks: ") and "MCU" in c1 and len(c1) < 90, c1
    assert str(sexpr.value(tb, "rev", "")), "revision is filled"
    before = pr.sch.read_bytes()
    r2 = engine.relayout(pr)
    assert r2["style_used"] in ("pro", "unchanged") and pr.sch.read_bytes() == before, r2["style_used"]


def test_docked_connector_pin_jumpers_and_bank_rows():
    """A small connector that only talks to one IC is drawn in that IC's block (no frame of its own); the doubled
    USB-C data pins are joined by a jumper at the connector and carry ONE name; a long decoupling bank wraps into
    rows of at most six; the sheet does not grow a paper size for any of it."""
    pr = _fixture("rp2040-min", "dock")
    D = Design(Schematic.load(pr.sch, pr.name), export_netlist(pr))
    assert "J2" not in {b.id for b in D.blocks} and D.block_of["J2"] == "U1", D.block_of.get("J2")
    assert "J1" in {b.id for b in D.blocks} and "J3" in {b.id for b in D.blocks}      # supply input, 20-pin header
    r = engine.relayout(pr, force=True)
    assert r["style_used"] == "pro" and r["paper"] == "A3", (r["style_used"], r.get("paper"), r.get("tried"))
    sch = Schematic.load(pr.sch, pr.name)
    groups = O.geo_groups(sch)
    for a, b in (("A6", "B6"), ("A7", "B7")):
        g = [g for g in groups if ("J1", a) in {tuple(p[:2]) for p in g["pins"]}]
        assert len(g) == 1 and ("J1", b) in {tuple(p[:2]) for p in g[0]["pins"]}, (a, b)
        assert len(g[0]["labels"]) == 1, (a, g[0]["labels"])
    caps = [u for u in sch.symbols(include_power=False) if u.ref.startswith("C") and D.block_of.get(u.ref) == "U1"]
    rows: dict = {}
    for u in caps:
        rail = sorted(k for k in D.pins_of[u.ref].values() if not D.is_gnd(k))[0]
        rows.setdefault((rail, round(u.at[1], 2)), []).append(u.ref)
    assert rows and max(len(v) for v in rows.values()) <= 6, rows


def _run_all():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    failed = 0
    for n, f in tests:
        if only and not any(o in n for o in only):
            continue
        t = time.time()
        try:
            f()
            print(f"PASS {n} ({time.time() - t:.1f}s)", flush=True)
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc(limit=4)
    print(f"\n{'FAILED ' + str(failed) if failed else 'all passed'} - artifacts in {TMP}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
