"""Professional schematic layout + netlist-identity gate - run against the real KiCad 10 install.

    python tests/test_schematic_pro.py            (plain runner)

Every "nothing changed electrically" claim is made by kicad-cli (whole-project netlist + ERC) on staged copies,
never by Copper's own model. Fixtures: four Copper-built boards in tests/data/pro, plus KiCad's demos.
"""
from __future__ import annotations

import json
import os
import re
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
from copper_kicad.sch.pro import engine, gate, score  # noqa: E402
from copper_kicad.sch.pro import tool as SP  # noqa: E402
from copper_kicad.sch.pro.blocks import Design  # noqa: E402
import copper_kicad.tools  # noqa: E402,F401  (registers the tools)

TMP = Path(tempfile.mkdtemp(prefix="copper-pro-test-"))
DATA = ROOT / "tests" / "data" / "pro"
BOARDS = ["c3-devboard", "rp2040-min", "stm32-sensor", "li-ion-powerbank"]
_ENV = {"lib_symbol_issues", "footprint_link_issues", "lib_symbol_mismatch"}


def _board(name: str, tag: str = "") -> Project:
    d = TMP / (tag or name) / name
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(DATA / name, d)
    return Project(d, name)


def _demo(dname: str, tag: str) -> Project:
    d = TMP / tag
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(env.kicad().share / "demos" / dname, d)
    return Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)


def _code(fn):
    try:
        fn()
    except ToolError as e:
        return e.code
    return None


def _tree_listing(d: Path) -> list:
    return sorted((str(p.relative_to(d)), p.stat().st_size, p.stat().st_mtime_ns) for p in d.rglob("*") if p.is_file())


def _counts(sch: Schematic) -> dict:
    return {k: len(sch.items(k)) for k in ("rectangle", "text", "group", "wire", "label", "junction", "no_connect")}


_TIDY: dict = {}


def _tidied(name: str):
    """One pro tidy per board, shared by the read-only tests: (project, result, wall seconds)."""
    if name not in _TIDY:
        pr = _board(name, "tidied")
        nl0 = export_netlist(pr)
        t = time.time()
        r = engine.relayout(pr)
        _TIDY[name] = (pr, r, time.time() - t, nl0)
    return _TIDY[name]


# ── the gate ──────────────────────────────────────────────────────────────
def test_gate_identity_noop():
    """Re-serialising a sheet through Copper's model must pass G0-G4 with equal hashes (flat, hierarchical,
    multi-instance, old file format)."""
    cases = [(_board("c3-devboard", "noop"), None), (_demo("pic_programmer", "noop_pic"), "pic_sockets.kicad_sch"),
             (_demo("complex_hierarchy", "noop_cx"), "ampli_ht.kicad_sch"), (_demo("ecc83", "noop_ecc"), None)]
    for pr, sheet in cases:
        f, spath = O.sheet_file(pr, sheet)
        b = gate.baseline(pr, f, spath)
        try:
            g = gate.check(pr, b, Schematic.load(f, pr.name, spath).text(), spath)
            assert g.ok and g.hash_before == g.hash_after, (pr.name, g.reasons)
            assert b.cli_calls <= 6, b.cli_calls
        finally:
            b.close()


def _mutations(pr, sheet):
    """(name, mutated text, must be refused, expected gate letters)."""
    f, spath = O.sheet_file(pr, sheet)

    def load():
        return Schematic.load(f, pr.name, spath)
    out = []
    s = load()
    w = s.wires()
    tips = {(round(p["xy"][0], 2), round(p["xy"][1], 2)) for x in s.symbols(include_power=False) for p in x.pins_world()}
    cand = next((x for x in w if (round(x["a"][0], 2), round(x["a"][1], 2)) in tips
                 or (round(x["b"][0], 2), round(x["b"][1], 2)) in tips), w[0])
    s.remove(cand["node"])
    out.append(("delete a wire at a pin", s.text(), True, "G1 G2 G3 G4"))
    s = load()
    lab = next((l for l in s.labels() if l["kind"] == "local"), None) or s.labels()[0]
    lab["node"][1] = lab["net"] + "_X"
    out.append(("rename a label", s.text(), True, "G1 G2 G3 G4"))
    s = load()
    fl = next((x for x in s.symbols() if O.is_flag(x)), None)
    if fl is not None:
        a = sexpr.find(fl.node, "at")
        a[1], a[2] = float(a[1]) + 1.27 * 37, float(a[2]) + 1.27 * 41
        out.append(("move a PWR_FLAG off its net", s.text(), True, "G2 G3 G4"))
    s = load()
    nc = s.items("no_connect")
    if nc:
        s.remove(nc[0])
        out.append(("delete a no-connect flag", s.text(), True, "G1 G2 G4"))
    s = load()
    big = max(s.symbols(include_power=False), key=lambda x: len(x.unit_pins()))
    a = sexpr.find(big.node, "at")
    a[1] = float(a[1]) + 2.54
    out.append(("move a symbol off its wires", s.text(), True, "G1 G2 G3 G4"))
    s = load()
    sy = s.symbols(include_power=False)[0]
    d = sexpr.find(sy.node, "dnp")
    if d is not None:
        d[1] = sexpr.Sym("yes" if str(d[1]) == "no" else "no")
        out.append(("flip dnp", s.text(), True, "G0"))
    return out


def test_gate_catches_mutations():
    for pr, sheet in ((_demo("pic_programmer", "mut_pic"), None), (_demo("pic_programmer", "mut_pic2"), "pic_sockets.kicad_sch"),
                      (_demo("complex_hierarchy", "mut_cx"), "ampli_ht.kicad_sch"), (_board("c3-devboard", "mut"), None)):
        f, spath = O.sheet_file(pr, sheet)
        before = f.read_bytes()
        b = gate.baseline(pr, f, spath)
        try:
            for name, text, refuse, letters in _mutations(pr, sheet):
                g = gate.check(pr, b, text, spath)
                assert g.ok != refuse, (pr.name, sheet, name, g.reasons)
                assert any(r.split()[0] in letters.split() for r in g.reasons), (name, g.reasons)
                if name == "rename a label":
                    assert any(r.startswith(("G1", "G3")) for r in g.reasons), g.reasons
        finally:
            b.close()
        assert f.read_bytes() == before


def test_label_inventory_equality():
    """Renaming ANY single label must be refused - also the redundant alias labels of a hand-drawn hierarchical
    leaf, which KiCad's netlist does not show (G3: the set of names on every net is compared for equality)."""
    for pr, sheet, limit in ((_demo("pic_programmer", "g3_pic"), "pic_sockets.kicad_sch", 400),
                             (_demo("complex_hierarchy", "g3_cx"), "ampli_ht.kicad_sch", 400),
                             (_board("c3-devboard", "g3"), None, 12)):
        f, spath = O.sheet_file(pr, sheet)
        b = gate.baseline(pr, f, spath)
        try:
            n = len(Schematic.load(f, pr.name, spath).labels())
            assert n, sheet
            blind = 0
            for i in range(min(n, limit)):
                s = Schematic.load(f, pr.name, spath)
                l = s.labels()[i]
                l["node"][1] = l["net"] + "_RENAMED"
                g = gate.check(pr, b, s.text(), spath)
                assert not g.ok, (sheet, l["net"], l["kind"])
                blind += not any(r.startswith("G1") for r in g.reasons)
            print(f"   {f.name}: {min(n, limit)} label renames refused, {blind} of them invisible to the netlist")
        finally:
            b.close()


def test_gate_leaves_project_untouched():
    """Space in the path, sub-sheet in a sub-folder: the project directory is not written to (no .kicad_prl)."""
    src = env.kicad().share / "demos" / "pic_programmer"
    d = TMP / "sp ace" / "proj dir"
    shutil.rmtree(d, ignore_errors=True)
    (d / "sub").mkdir(parents=True)
    for fn in ("pic_programmer.kicad_sch", "pic_programmer.kicad_pro", "pic_programmer.kicad_sym", "sym-lib-table"):
        if (src / fn).exists():
            shutil.copy(src / fn, d / fn)
    shutil.copy(src / "pic_sockets.kicad_sch", d / "sub" / "pic_sockets.kicad_sch")
    root = d / "pic_programmer.kicad_sch"
    root.write_text(root.read_text(encoding="utf-8").replace('"pic_sockets.kicad_sch"', '"sub/pic_sockets.kicad_sch"'),
                    encoding="utf-8", newline="\n")
    pr = Project(d, "pic_programmer")
    listing = _tree_listing(d)
    f, spath = O.sheet_file(pr, "pic_sockets")
    assert f.parent.name == "sub"
    b = gate.baseline(pr, f, spath)
    try:
        assert (b.stage_dir / "sub" / "pic_sockets.kicad_sch").exists()
        g = gate.check(pr, b, Schematic.load(f, pr.name, spath).text(), spath)
        assert g.ok, g.reasons
    finally:
        b.close()
    assert _tree_listing(d) == listing


def test_duplicate_refs_refused():
    pr = _board("c3-devboard", "dup")
    t = pr.sch.read_text(encoding="utf-8")
    assert '(reference "R2")' in t
    pr.sch.write_text(t.replace('(reference "R2")', '(reference "R1")').replace('(property "Reference" "R2"', '(property "Reference" "R1"'),
                      encoding="utf-8", newline="\n")
    before = pr.sch.read_bytes()
    for lang in ("C", "fr_FR.UTF-8"):
        old = os.environ.get("LANG")
        os.environ["LANG"] = lang
        try:
            assert _code(lambda: engine.relayout(pr)) == "LAYOUT_UNSUPPORTED"
        finally:
            if old is None:
                os.environ.pop("LANG", None)
            else:
                os.environ["LANG"] = old
    assert pr.sch.read_bytes() == before


def test_symbol_payload_invariance():
    """G0: data the default netlist does not carry (instances of other paths, label fields) may not change."""
    pr = _board("c3-devboard", "g0")
    b = gate.baseline(pr, pr.sch)
    try:
        s = Schematic.load(pr.sch, pr.name)
        sy = s.symbols(include_power=False)[0]
        inst = sexpr.find(sy.node, "instances")
        proj = sexpr.find(inst, "project")
        proj.append([sexpr.Sym("path"), "/00000000-0000-0000-0000-000000000001", [sexpr.Sym("reference"), "R99"], [sexpr.Sym("unit"), 1]])
        g = gate.check(pr, b, s.text())
        assert not g.ok and any(r.startswith("G0") for r in g.reasons), g.reasons
        s = Schematic.load(pr.sch, pr.name)
        lab = s.labels()[0]["node"]
        eff = sexpr.find(lab, "effects")
        font = sexpr.find(eff, "font")
        sexpr.find(font, "size")[1:] = [2.54, 2.54]
        g = gate.check(pr, b, s.text())
        assert not g.ok and any(r.startswith("G0") for r in g.reasons), g.reasons
    finally:
        b.close()


def test_gate_refuses_and_writes_nothing():
    """An engine that draws a short must be stopped: lower rung or LAYOUT_REFUSED, never a changed netlist."""
    pr = _board("c3-devboard", "short")
    before = pr.sch.read_bytes()
    nl0 = export_netlist(pr)
    orig = E.emit_wires

    def bad(sch, wires, keep, pin_tips):
        r = orig(sch, wires, keep, pin_tips)
        tips = sorted((c, n) for c, n in pin_tips.items() if n)
        (ca, na) = tips[0]
        (cb, nb) = next(t for t in tips if t[1] != na)             # two pins of different nets, joined by an L wire
        pa, pb = (ca[0] * 1.27, ca[1] * 1.27), (cb[0] * 1.27, cb[1] * 1.27)
        sch.add_wire(pa, (pb[0], pa[1]))
        sch.add_wire((pb[0], pa[1]), pb)
        return r
    E.emit_wires = bad
    orig_verify = engine.route.verify
    engine.route.verify = lambda sch, D: []          # blind the pre-check too: the kicad-cli gate must catch it alone
    orig_part = engine.route.verify_partition       # ... and the union-find pre-gate (pro2 work package 1)
    engine.route.verify_partition = lambda sch, D, before=None: []
    try:
        try:
            r = engine.relayout(pr)
            assert r["style_used"] == "labels" and r.get("tried"), r
            assert all(any(x.startswith(("G1", "G2", "G3", "G4")) for x in t["refused"]) for t in r["tried"]), r["tried"]
        except ToolError as e:
            assert e.code == "LAYOUT_REFUSED", e.code
            assert pr.sch.read_bytes() == before
            assert not (pr.dir / ".copper" / "snapshots").exists() or not list((pr.dir / ".copper" / "snapshots").iterdir())
    finally:
        E.emit_wires = orig
        engine.route.verify = orig_verify
        engine.route.verify_partition = orig_part
    assert gate.canon_hash(_canon(pr)) == gate.canon_hash(_canon_of(nl0, pr, before))


def _canon(pr):
    td = gate.stage(pr)
    try:
        return gate.canon(gate.export_tree(td / pr.sch.name))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _canon_of(_nl, pr, raw: bytes):
    td = gate.stage(pr)
    try:
        (td / pr.sch.name).write_bytes(raw)
        return gate.canon(gate.export_tree(td / pr.sch.name))
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── the layout ────────────────────────────────────────────────────────────
def test_relayout_identical_netlist():
    """All four boards: pro style accepted by the gate, deterministic, and a second run does not stack anything."""
    for name in BOARDS:
        pr, r, _, _ = _tidied(name)
        assert r["style_used"] == "pro", (name, r.get("tried"))
        assert "identical" in r["connectivity"], r
        assert r["erc"]["after"]["error"] <= r["erc"]["before"]["error"], r["erc"]
        s1 = Schematic.load(pr.sch, pr.name)
        assert E.is_engine_layout(s1), name
        assert engine.sheet_state(s1) == "engine"
        # determinism: same input -> same bytes
        pr2 = _board(name, "det")
        r2 = engine.relayout(pr2)
        assert r2["style_used"] == "pro"
        assert pr2.sch.read_bytes() == pr.sch.read_bytes(), f"{name}: two runs on the same input differ"
        # idempotence: nothing stacks
        c1 = _counts(Schematic.load(pr2.sch, pr2.name))
        r3 = engine.relayout(pr2)
        assert r3["style_used"] in ("pro", "unchanged"), r3
        c2 = _counts(Schematic.load(pr2.sch, pr2.name))
        assert c1 == c2, (name, c1, c2)


# measured after-values on 2026-11-09 (score, labels/net, sat distance); the floors sit just under them and are a
# ratchet: raise them when the engine improves, never lower them without a CHANGELOG note
# 2026-11: rp2040-min part distance 45 -> 49 (its ten decoupling caps now stand in one row AT the IOVDD pins and the
# crystal sits at the end of the XIN wire: further from the pin, but on the wire - see CHANGELOG); li-ion-powerbank
# now fits A4. The score itself got stricter (sideways supply symbols, vertical names, off-centre drawings count).
FLOOR = {"c3-devboard": (72, 1.9, 33, "A4"), "rp2040-min": (63, 2.05, 49, "A3"),
         "stm32-sensor": (77, 1.8, 36, "A4"), "li-ion-powerbank": (80, 1.8, 31, "A4")}


def test_readability_floor():
    for name in BOARDS:
        pr, r, _, _ = _tidied(name)
        out = registry.call_tool("sch_read", {"project": str(pr.pro), "query": "readability"})
        m = out["structuredContent"]
        fl = FLOOR[name]
        assert m["state"] == "engine"
        assert m["offgrid_pins"] == 0, name
        assert m["frames"] == m["blocks"] >= 3, (name, m["frames"], m["blocks"])
        assert m["overlaps"] == 0, (name, m["overlap_list"])
        assert m["wire_through_text_or_body"] <= 2, (name, m["wire_through_text_or_body"])
        assert m["wire_crossings"] <= 3, (name, m["wire_crossings"])
        assert m["labels_per_signal_net"] <= fl[1], (name, m["labels_per_signal_net"])
        assert m["sat_dist_mean_mm"] <= fl[2], (name, m["sat_dist_mean_mm"])
        # `score` is score v2 since pro2 work package 1; the floors of this test were set on the earlier
        # composite, which is still reported as score_v1
        assert m["score_v1"] >= fl[0], (name, m["score_v1"])
        assert m["paper"] == fl[3], (name, m["paper"])
        assert m["sheet_fill"] >= 0.3, (name, m["sheet_fill"])
        before = r["readability"]["before"]["score_v1"]
        assert m["score_v1"] >= before + 20, (name, before, m["score_v1"])
        assert 0 <= m["score"] <= 100 and isinstance(m["hard"], list)
        assert r["readability"]["after"]["labels"] < r["readability"]["before"]["labels"]


def _wired(sch, a: str, b: str) -> bool:
    ra, rb = a.split("."), b.split(".")
    for g in O.geo_groups(sch):
        refs = {p[0] for p in g["pins"]}
        pins = set(g["pins"])
        ok_a = (ra[0] in refs) if len(ra) == 1 else ((ra[0], ra[1]) in pins)
        ok_b = (rb[0] in refs) if len(rb) == 1 else ((rb[0], rb[1]) in pins)
        if ok_a and ok_b:
            return True
    return False


def _dist(sch, a: str, pin: str) -> float:
    s = sch.find_ref(a)[0]
    r, n = pin.split(".")
    p = next(p for p in sch.find_ref(r)[0].pins_world() if p["number"] == n)
    return abs(s.at[0] - p["xy"][0]) + abs(s.at[1] - p["xy"][1])


def test_specific_adjacency_dbg():
    """The things a reviewer looks for on the ESP32-C3 board: parts drawn at the pin they serve, joined by wire."""
    pr, _, _, _ = _tidied("c3-devboard")
    sch = Schematic.load(pr.sch, pr.name)
    assert _wired(sch, "D1", "R5") and _dist(sch, "D1", "R5.1") < 25
    for c in ("C3", "C4"):
        # on the supply pin's own wire (pin - rail symbol - caps), not in a bank that merely floats nearby
        assert _wired(sch, c, "U2.3") and _dist(sch, c, "U2.3") < 60, (c, _dist(sch, c, "U2.3"))
    assert _wired(sch, "C3", "C4")                       # one decoupling bank
    for part in ("R3", "C5", "SW1"):
        assert _wired(sch, part, "U2.8"), part           # EN pull-up, RC cap, reset switch on the EN wire
    assert _wired(sch, "R4", "U2.23") and _wired(sch, "SW2", "U2.23")
    assert _wired(sch, "C1", "U1.3") and _wired(sch, "C2", "U1.2")
    assert _wired(sch, "R1", "J1") and _wired(sch, "R2", "J1")
    titles = [str(t[1]) for t in sch.items("text")]
    assert any(t.startswith("MCU") for t in titles) and any("VBUS → +3V3" in t for t in titles), titles
    tb = sexpr.find(sch.tree, "title_block")
    assert str(sexpr.value(tb, "title", "")) == "C3 Devboard" and re.match(r"\d{4}-\d\d-\d\d", str(sexpr.value(tb, "date", "")))


def test_power_orientation():
    for name in BOARDS:
        pr, _, _, _ = _tidied(name)
        sch = Schematic.load(pr.sch, pr.name)
        m = score.measure(sch, export_netlist(pr))
        assert m["power_upright"] >= 0.7 * m["power_symbols"], (name, m["power_upright"], m["power_symbols"])
        for s in sch.symbols():
            if s.is_power and not O.is_flag(s):
                bb, tip = s.body_bbox(), s.pins_world()[0]["xy"]
                dy = (bb[1] + bb[3]) / 2 - tip[1]
                dx = (bb[0] + bb[2]) / 2 - tip[0]
                if abs(dy) > abs(dx):
                    from copper_kicad.sch.layout import is_ground
                    assert (dy > 0) == is_ground(s.value), f"{name}: {s.value} symbol is upside down"


def test_pwr_flags_and_nc():
    for name in BOARDS:
        pr, r, _, nl0 = _tidied(name)
        s0 = Schematic.load(DATA / name / f"{name}.kicad_sch", name)
        s1 = Schematic.load(pr.sch, pr.name)
        nl1 = export_netlist(pr)
        assert gate.flag_counts(s0, nl0) == gate.flag_counts(s1, nl1) and gate.flag_counts(s0, nl0), name
        assert gate.nc_pins(s0) == gate.nc_pins(s1), name
        assert nl0["nets"].keys() == nl1["nets"].keys()
    pr, _, _, nl0 = _tidied("c3-devboard")
    assert sum(gate.flag_counts(Schematic.load(pr.sch, pr.name), nl0).values()) == 2
    assert len(gate.nc_pins(Schematic.load(pr.sch, pr.name))) == 19


def test_frames_marker():
    """Frames and titles belong to copper:block:* groups; a user's own rectangle and note survive re-layouts."""
    pr = _board("c3-devboard", "frames")
    engine.relayout(pr)
    s = Schematic.load(pr.sch, pr.name)
    own = E.copper_member_uuids(s)
    rects = s.items("rectangle")
    assert rects and all(str(sexpr.value(n, "uuid")) in own for n in rects + s.items("text"))
    assert score.frame_count(s) == len(rects)
    s._insert([sexpr.Sym("rectangle"), [sexpr.Sym("start"), 20, 180], [sexpr.Sym("end"), 40, 190],
               [sexpr.Sym("stroke"), [sexpr.Sym("width"), 0], [sexpr.Sym("type"), sexpr.Sym("default")]],
               [sexpr.Sym("fill"), [sexpr.Sym("type"), sexpr.Sym("none")]], [sexpr.Sym("uuid"), "11111111-2222-3333-4444-555555555555"]])
    s.save(pr.sch)
    assert engine.sheet_state(Schematic.load(pr.sch, pr.name)) == "hand"       # a human touched it
    r = engine.relayout(pr)
    assert r["style_used"] == "kept" and r["orphan_graphics"] == 1, r
    for _ in range(2):
        r = engine.relayout(pr, force=True)
        assert r["style_used"] in ("pro", "pro-plain", "unchanged"), r
        s = Schematic.load(pr.sch, pr.name)
        mine = [n for n in s.items("rectangle") if str(sexpr.value(n, "uuid")) == "11111111-2222-3333-4444-555555555555"]
        assert len(mine) == 1 and len(s.items("rectangle")) == len(rects) + 1


def test_hierarchy_and_multi_instance():
    """Leaf sheet of a hand-drawn hierarchy: kept without force, accepted by the gate with force (hierarchical
    labels moved, not re-created). Sheet symbols / shared sheets: refused, byte-identical."""
    pr = _demo("pic_programmer", "hier_pic")
    f = pr.dir / "pic_sockets.kicad_sch"
    before = f.read_bytes()
    s0 = Schematic.load(f, pr.name)
    hl0 = sorted((l["net"], str(sexpr.value(l["node"], "shape", ""))) for l in s0.labels() if l["kind"] == "hierarchical")
    hu0 = sorted(str(sexpr.value(l["node"], "uuid")) for l in s0.labels() if l["kind"] == "hierarchical")
    assert len(hl0) == 4
    r = engine.relayout(pr, "pic_sockets.kicad_sch")
    assert r["style_used"] == "kept" and f.read_bytes() == before
    canon0 = _canon(pr)
    r = engine.relayout(pr, "pic_sockets.kicad_sch", force=True)
    assert r["style_used"] in ("pro", "pro-plain"), r
    assert "4 hierarchical" in r["connectivity"] and "PWR_FLAG" in r["connectivity"], r["connectivity"]
    s1 = Schematic.load(f, pr.name)
    assert sorted((l["net"], str(sexpr.value(l["node"], "shape", ""))) for l in s1.labels() if l["kind"] == "hierarchical") == hl0
    assert sorted(str(sexpr.value(l["node"], "uuid")) for l in s1.labels() if l["kind"] == "hierarchical") == hu0
    assert _canon(pr) == canon0
    root_before = pr.sch.read_bytes()
    assert _code(lambda: engine.relayout(pr, force=True)) == "LAYOUT_UNSUPPORTED"       # root has sheet symbols
    assert pr.sch.read_bytes() == root_before
    pr = _demo("complex_hierarchy", "hier_cx")
    f = pr.dir / "ampli_ht.kicad_sch"
    before = f.read_bytes()
    assert _code(lambda: engine.relayout(pr, "ampli_ht.kicad_sch", force=True)) == "LAYOUT_UNSUPPORTED"
    assert f.read_bytes() == before
    pr = _demo("ecc83", "hier_ecc")                      # hand-drawn analog sheet: never touched implicitly
    before = pr.sch.read_bytes()
    assert engine.relayout(pr)["style_used"] == "kept" and pr.sch.read_bytes() == before
    try:                                                 # forced: either proven or refused, never silently different
        engine.relayout(pr, force=True)
        ok = True
    except ToolError as e:
        ok = False
        assert e.code in ("LAYOUT_REFUSED", "LAYOUT_UNSUPPORTED") and pr.sch.read_bytes() == before


def test_tool_surface_and_performance():
    """sch_tidy through the tool registry: compact result, proof string, ERC, budget of kicad-cli calls and time."""
    pr = _board("c3-devboard", "tool")
    out = registry.call_tool("sch_tidy", {"project": str(pr.pro), "preview": False, "dry_run": True})
    d = out["structuredContent"]
    assert d["style_used"] == "pro2" and d["dry_run"] and "revision" not in d        # pro2 is the tool's default
    assert pr.sch.read_bytes() == (DATA / "c3-devboard" / "c3-devboard.kicad_sch").read_bytes()
    t = time.time()
    out = registry.call_tool("sch_tidy", {"project": str(pr.pro), "preview": False,
                                          "title_block": {"rev": "B", "company": "ACME"},
                                          "blocks": [{"id": "U2", "title": "RADIO MCU", "note": "3V3 only"}]})
    wall = time.time() - t
    d = out["structuredContent"]
    assert d["style_used"] == "pro2" and d["paper"] == "A4" and "identical" in d["connectivity"] and "fallback" not in d, d
    assert d["readability"]["after"] >= d["readability"]["before"] + 20
    assert "errors" in d["verify"]["erc"]
    body = {k: v for k, v in d.items() if k != "verify"}
    assert len(json.dumps(body)) <= 1200, len(json.dumps(body))
    s = Schematic.load(pr.sch, pr.name)
    tb = sexpr.find(s.tree, "title_block")
    assert str(sexpr.value(tb, "rev")) == "B" and str(sexpr.value(tb, "company")) == "ACME"
    texts = [str(t[1]) for t in s.items("text")]
    assert "RADIO MCU" in texts and "3V3 only" in texts, texts
    assert wall < 12, wall
    for name, limit in (("c3-devboard", 6.0), ("rp2040-min", 8.0)):
        pr2, r, sec, _ = _tidied(name)
        assert r["cli_calls"] <= 6, r["cli_calls"]
        assert r["route"]["seconds"] <= 1.0 and r["route"]["expanded"] <= 40000 * max(1, r["route"]["requests"])
        assert sec <= limit, (name, sec)
    # the older label style is still available and goes through the same gate
    pr3 = _board("stm32-sensor", "labels")
    d = registry.call_tool("sch_tidy", {"project": str(pr3.pro), "preview": False, "style": "labels"})["structuredContent"]
    assert d["style_used"] == "labels" and "identical" in d["connectivity"], d


def test_implicit_relayout_respects_hand_edits():
    """sch_apply re-lays a sheet only while nobody but the engine has arranged it; explicit positions are kept."""
    d = TMP / "implicit"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    pr = Project(d, "imp")
    ops = [{"op": "place", "lib_id": "Regulator_Linear:AMS1117-3.3", "id": "ldo"},
           {"op": "place", "lib_id": "Device:C", "value": "10uF", "id": "cin"},
           {"op": "place", "lib_id": "Device:C", "value": "22uF", "id": "cout"},
           {"op": "place", "lib_id": "Device:R", "value": "1k", "id": "r"},
           {"op": "place", "lib_id": "Device:LED", "id": "led"},
           {"op": "place", "lib_id": "Connector_Generic:Conn_01x02", "id": "j"},
           {"op": "connect", "pins": ["ldo.VI", "cin.1", "j.1"], "net": "VBUS"},
           {"op": "connect", "pins": ["ldo.GND", "cin.2", "cout.2", "j.2", "led.K"], "net": "GND"},
           {"op": "connect", "pins": ["ldo.VO", "cout.1", "r.1"], "net": "+3V3"},
           {"op": "connect", "pins": ["r.2", "led.A"], "net": "LED_A"},
           {"op": "pwr_flag", "net": "VBUS"}, {"op": "pwr_flag", "net": "GND"}]
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "ops": ops, "preview": False})
    d1 = out["structuredContent"]
    assert d1.get("layout", "").startswith("re-laid"), d1.get("layout")
    assert d1["connectivity"]["ok"] == d1["connectivity"]["checked"], d1["connectivity"]
    s = Schematic.load(pr.sch, pr.name)
    assert E.is_engine_layout(s) and score.frame_count(s) >= 2
    assert d1["revision"] == __import__("copper_kicad.project", fromlist=["revision"]).revision(pr.sch)
    # an explicit position is never overridden
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "preview": False,
                                           "ops": [{"op": "place", "lib_id": "Device:R", "ref": "R9", "at": [200.66, 30.48]}]})
    assert "layout" not in out["structuredContent"], out["structuredContent"].get("layout")
    s = Schematic.load(pr.sch, pr.name)
    assert [round(v, 2) for v in s.find_ref("R9")[0].at] == [200.66, 30.48]
    assert engine.sheet_state(s) != "engine"             # the sheet now counts as hand-arranged
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "preview": False,
                                           "ops": [{"op": "place", "lib_id": "Device:C", "ref": "C9"}]})
    assert out["structuredContent"]["layout"].startswith("kept"), out["structuredContent"]
    s = Schematic.load(pr.sch, pr.name)
    assert [round(v, 2) for v in s.find_ref("R9")[0].at] == [200.66, 30.48]
    # layout='keep' never re-lays; sch_tidy brings the sheet back under the engine
    r = engine.relayout(pr, force=True)
    assert r["style_used"] in ("pro", "pro-plain"), r
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "preview": False, "layout": "keep",
                                           "ops": [{"op": "place", "lib_id": "Device:C", "ref": "C10"}]})
    assert "layout" not in out["structuredContent"]


def test_block_detection():
    want = {"c3-devboard": {"U2": {"C3", "C4", "C5", "D1", "R3", "R4", "R5", "SW1", "SW2"}, "U1": {"C1", "C2"}, "J1": {"R1", "R2"}},
            "stm32-sensor": {"U3": {"C4", "C7"}, "J1": {"Q1", "R3"}},     # reverse-protection FET: with the battery input
            "li-ion-powerbank": {"U3": {"C4", "D1", "L1", "R8", "R9"}, "U2": {"C3", "R6", "R7"}}}
    for name, blocks in want.items():
        pr = _board(name, "det")
        D = Design(Schematic.load(pr.sch, pr.name), export_netlist(pr))
        got = {b.id: set(b.sats) for b in D.blocks}
        for anchor, sats in blocks.items():
            assert got.get(anchor) == sats, (name, anchor, got.get(anchor))
        assert [b.rank for b in D.blocks] == sorted(b.rank for b in D.blocks)
    pr = _demo("pic_programmer", "det_pic")
    f, spath = O.sheet_file(pr, "pic_sockets")
    D = Design(Schematic.load(f, pr.name, spath), export_netlist(pr))
    hier = {e[0] for ln in D.view.nets.values() for e in ln.names if e[1] == "hierarchical"}
    assert hier == {"VCC_PIC", "VPP-MCLR", "CLOCK-RB6", "DATA-RB7"}, hier
    assert all(r in D.units for k, ln in D.view.nets.items() for r, _ in ln.pins)       # only this sheet's parts


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
            print(f"PASS {n} ({time.time() - t:.1f}s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)
    print(f"\n{'FAILED ' + str(failed) if failed else 'all passed'} - artifacts in {TMP}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
