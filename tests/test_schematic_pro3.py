"""Tests of the "pro2" schematic engine, work packages 1 to 3 (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md):
score v2, the union-find pre-gate, the idiom layer (Match / Template / instantiate), the passive idioms, upright
power symbols, grouped grounds and the pro2 rung of the style ladder.

    python tests/test_schematic_pro3.py [name filter]        (plain runner, real KiCad 10 install)

The designs are the seven of bench/sch/corpus (copied to a temp dir; the corpus is never modified).
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
os.environ.setdefault("KICAD_CONFIG_HOME", tempfile.mkdtemp(prefix="copper-kicfg-"))

from copper_kicad import sexpr  # noqa: E402
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.model import Schematic  # noqa: E402
from copper_kicad.sch.netlist import export_netlist  # noqa: E402
from copper_kicad.sch.pro import build as B  # noqa: E402
from copper_kicad.sch.pro import engine, exemplar, route, score  # noqa: E402
from copper_kicad.sch.pro.blocks import Design  # noqa: E402
from copper_kicad.sch.pro.idioms import match as IM  # noqa: E402
from copper_kicad.sch.pro.idioms import model as IMOD  # noqa: E402
from copper_kicad.sch.pro.idioms import passive as IP  # noqa: E402
from copper_kicad.sch.pro.idioms import stage as ST  # noqa: E402
from copper_kicad.sch.pro import dock as DOCK  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="copper-pro3-"))
CORPUS = ROOT / "bench" / "sch" / "corpus"
BOARDS = sorted(d.name for d in CORPUS.iterdir() if d.is_dir() and list(d.glob("*.kicad_sch")))
_CACHE: dict = {}


def _board(name: str, tag: str) -> Project:
    d = TMP / f"{name}-{tag}"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(CORPUS / name, d)
    return Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)


def _laid(name: str):
    """(project, engine result, Design of the ORIGINAL sheet, signature of the original) after relayout pro2."""
    if name not in _CACHE:
        pr = _board(name, "pro2")
        sch0 = O.load_sheet(pr, None, create=False)
        D0 = Design(sch0, export_netlist(pr))
        sig0 = route.signature(sch0)
        r = engine.relayout(pr, force=True, style="pro2")
        _CACHE[name] = (pr, r, D0, sig0)
    return _CACHE[name]


def _memory_layout(name: str, tag: str):
    """A pro2 layout built in memory (nothing gated, nothing written): (sch, Design, original signature)."""
    pr = _board(name, tag)
    nl = export_netlist(pr)
    sig0 = route.signature(O.load_sheet(pr, None, create=False))
    sch = O.load_sheet(pr, None, create=False)
    D = Design(sch, nl)
    engine.layout_pro(sch, D, pr.name, v2=True)
    return sch, D, sig0


def _memory_layout_unseen(name: str, tag: str):
    """As _memory_layout, for a design of bench/sch/corpus_unseen."""
    d = TMP / f"{name}-{tag}"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(ROOT / "bench" / "sch" / "corpus_unseen" / name, d)
    pr = Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)
    nl = export_netlist(pr)
    sig0 = route.signature(O.load_sheet(pr, None, create=False))
    sch = O.load_sheet(pr, None, create=False)
    D = Design(sch, nl)
    engine.layout_pro(sch, D, pr.name, v2=True)
    return sch, D, sig0


def _reload(sch: Schematic) -> Schematic:
    return Schematic(sexpr.loads(sch.text()), sch.path, sch.project_name, sch.sheet_path)


# ───────────────────────── style ladder + identity ─────────────────────────
def test_pro2_is_used_and_netlist_is_identical():
    for name in BOARDS:
        pr, r, D0, sig0 = _laid(name)
        assert r["style_used"] == "pro2", (name, r.get("style_used"), r.get("tried"))
        assert "identical" in r["connectivity"], (name, r["connectivity"])
        assert not r.get("tried"), (name, r.get("tried"))
        nl1 = export_netlist(pr)
        D1 = Design(O.load_sheet(pr, None, create=False), nl1)
        assert {k: sorted(ln.pins) for k, ln in D0.view.nets.items()} == {k: sorted(ln.pins) for k, ln in D1.view.nets.items()}, name


def test_ladder_falls_to_pro_when_the_idiom_layer_breaks():
    """A bug in the new layer must cost one candidate, never the sheet: pro2 and pro2-safe refuse, pro wins."""
    pr = _board("c3-devboard", "ladder")
    orig = B.Blk.enable_v2

    def boom(self):
        raise RuntimeError("idiom layer broken on purpose")
    B.Blk.enable_v2 = boom
    try:
        r = engine.relayout(pr, force=True, style="pro2")
    finally:
        B.Blk.enable_v2 = orig
    assert r["style_used"] == "pro", r["style_used"]
    assert [t["style"] for t in r["tried"]] == ["pro2", "pro2-safe"], r["tried"]
    assert "identical" in r["connectivity"]


def test_every_idiom_dropped_still_gives_a_pro2_sheet():
    """Per-idiom fallback: when no template can be proven, the parts are drawn the legacy way inside pro2."""
    pr = _board("opamp-afe", "noidiom")
    orig = IMOD.instantiate

    def never(D, m, t, ctx_pins=None):
        raise IMOD.IdiomError("refused on purpose")
    IMOD.instantiate = never
    try:
        r = engine.relayout(pr, force=True, style="pro2")
    finally:
        IMOD.instantiate = orig
    assert r["style_used"] in ("pro2", "pro2-safe"), (r["style_used"], r.get("tried"))
    assert "identical" in r["connectivity"]
    # (class L: a bundle between a header and its IC is planned wiring, not a template - it needs no `instantiate`
    # and still counts the header as drawn by rule)
    assert r["idioms"]["claimed"] == (r["idioms"]["matches"].get("bundle") or 0) and r["idioms"]["dropped_count"] > 0, r["idioms"]


# ───────────────────────── union-find pre-gate ─────────────────────────
def test_pregate_has_no_false_reject_on_what_the_gate_accepted():
    for name in BOARDS:
        pr, r, D0, sig0 = _laid(name)
        sch1 = O.load_sheet(pr, None, create=False)
        assert route.signature(sch1, False) == route.signature(sch1, True), name      # unambiguous drawing
        errs = route.verify_partition(sch1, D0, sig0)
        assert errs == [], (name, errs[:3])
        assert route.verify_partition(sch1, D0) == [], name                           # also without the original


def _different_net_tips(sch, D):
    tips = []
    for s in sch.symbols(include_power=False):
        for p in s.pins_world():
            k = D.view.pin_net.get((s.ref, p["number"]))
            if k and D.conn(k):
                tips.append((tuple(p["xy"]), k))
    tips.sort()
    a = tips[0]
    b = next(t for t in tips if t[1] != a[1] and t[0][0] != a[0][0] and t[0][1] != a[0][1])
    return a, b


def test_pregate_catches_injected_faults():
    sch, D, sig0 = _memory_layout("c3-devboard", "faults")
    assert route.verify_partition(sch, D, sig0) == []
    # (1) a short: an L wire between two pins of different nets
    s1 = _reload(sch)
    (pa, na), (pb, nb) = _different_net_tips(s1, D)
    s1.add_wire(pa, (pb[0], pa[1]))
    s1.add_wire((pb[0], pa[1]), pb)
    e = route.verify_partition(s1, D, sig0)
    assert any("short" in x for x in e), e
    # (2) a dropped label: the net keeps its pins but loses a name (or falls apart)
    s2 = _reload(sch)
    lab = sorted(s2.labels(), key=lambda l: (l["net"], l["at"]))[0]
    assert s2.remove(lab["node"])
    e = route.verify_partition(s2, D, sig0)
    assert e and any(("names differ" in x) or ("differ from the original" in x) for x in e), e
    # (3) a moved PWR_FLAG: same number of flags on the sheet, but on another net
    s3 = _reload(sch)
    flag = next(s for s in s3.symbols() if O.is_flag(s))
    fpin = tuple(flag.pins_world()[0]["xy"])
    fnet = next(k for k, ln in D.view.nets.items() if ln.flags)
    target = None
    for s in s3.symbols(include_power=False):
        for p in s.pins_world():
            k = D.view.pin_net.get((s.ref, p["number"]))
            if k and D.conn(k) and not D.view.nets[k].flags:
                target = tuple(p["xy"])
                break
        if target:
            break
    B.set_pose(flag, flag.at[0] + target[0] - fpin[0], flag.at[1] + target[1] - fpin[1], flag.rot)
    e = route.verify_partition(s3, D, sig0)
    assert any("PWR_FLAG" in x for x in e), (fnet, e)
    # (4) a lost no-connect marker
    s4 = _reload(sch)
    ncs = s4.items("no_connect")
    if ncs:
        assert s4.remove(ncs[0])
        e = route.verify_partition(s4, D, sig0)
        assert any("no-connect" in x for x in e), e
    # (5) an ambiguous touch: a wire that ENDS on the middle of a foreign wire, no junction. KiCad versions
    # differ on whether that connects; the pre-gate refuses it either way
    s5 = _reload(sch)
    done = False
    for w in s5.wires():
        a, b = w["a"], w["b"]
        if abs(a[1] - b[1]) < 1e-6 and abs(a[0] - b[0]) >= 5.0:
            mid = (round((a[0] + b[0]) / 2 / 1.27) * 1.27, a[1])
            wk = next((k for (pt, k) in [(tuple(p["xy"]), D.view.pin_net.get((s.ref, p["number"])))
                                         for s in s5.symbols(include_power=False) for p in s.pins_world()]
                       if k and min(a[0], b[0]) - 1e-6 <= pt[0] <= max(a[0], b[0]) + 1e-6 and abs(pt[1] - a[1]) < 1e-6), None)
            (pa, na), _ = _different_net_tips(s5, D)
            if wk is None or wk == na:
                continue
            s5.add_wire(pa, (mid[0], pa[1]))
            s5.add_wire((mid[0], pa[1]), mid)
            done = True
            break
    assert done, "no horizontal wire to touch"
    e = route.verify_partition(s5, D, sig0)
    assert e, "a wire end on a foreign wire passed the pre-gate"


def test_pregate_runs_before_the_kicad_gate():
    """A candidate with a short is refused by the pre-gate (free) and never reaches kicad-cli."""
    from copper_kicad.sch.pro import emit as E
    from copper_kicad.sch.pro import gate
    pr = _board("c3-devboard", "pregate-order")
    orig, orig_verify, orig_check = E.emit_wires, engine.route.verify, gate.check
    calls = {"gate": 0}

    def bad(sch, wires, keep, pin_tips):
        r = orig(sch, wires, keep, pin_tips)
        tips = sorted((c, n) for c, n in pin_tips.items() if n)
        (ca, na) = tips[0]
        (cb, nb) = next(t for t in tips if t[1] != na)
        pa, pb = (ca[0] * 1.27, ca[1] * 1.27), (cb[0] * 1.27, cb[1] * 1.27)
        sch.add_wire(pa, (pb[0], pa[1]))
        sch.add_wire((pb[0], pa[1]), pb)
        return r

    def counting(*a, **k):
        calls["gate"] += 1
        return orig_check(*a, **k)
    E.emit_wires = bad
    engine.route.verify = lambda sch, D: []
    gate.check = counting
    before = pr.sch.read_bytes()
    try:
        r = engine.relayout(pr, force=True, style="pro2")
    finally:
        E.emit_wires, engine.route.verify, gate.check = orig, orig_verify, orig_check
    tried = {t["style"]: str(t["refused"]) for t in r.get("tried", [])}
    for rung in ("pro2", "pro2-safe", "pro", "pro-plain"):
        assert tried.get(rung, "").startswith("pre-gate"), tried
    assert r["style_used"] == "labels" and calls["gate"] == 1, (r["style_used"], calls)
    assert pr.sch.read_bytes() != before and "identical" in r["connectivity"]


# ───────────────────────── idiom layer ─────────────────────────
def test_matcher_is_read_only_and_finds_the_passive_idioms():
    want = {"c3-devboard": {"button", "led", "decap", "modgnd"}, "phone-breathalyzer": {"divider", "decap", "led"},
            "opamp-afe": {"rc", "decap", "pull"}, "li-ion-powerbank": {"led", "boost", "decap"}}   # WP2: the FB divider is the boost's
    for name, idioms in want.items():
        pr = _board(name, "match")
        sch = O.load_sheet(pr, None, create=False)
        D = Design(sch, export_netlist(pr))
        text0 = sch.text()
        md = IM.match_design(D)
        assert sch.text() == text0, name                                   # nothing was moved
        found = {m.idiom for m in md["matches"]}
        assert idioms <= found, (name, sorted(found))
        refs = [r for m in md["matches"] for r in m.refs()]
        assert len(refs) == len(set(refs)), (name, "a part was claimed twice")
        for m in md["matches"]:
            for role, k in m.nets.items():
                assert k in D.view.nets, (name, m.idiom, role)
    pr = _board("c3-devboard", "match2")
    D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
    mb = IM.match_block(D, next(b for b in D.blocks if b.id == "U2"))
    en = next(k for k in D.view.nets if D.text(k) == "EN")
    assert mb["node"][en].idiom == "button" and set(mb["node"][en].refs()) == {"R3", "C5", "SW1"}, mb["node"][en]
    led = next(m for m in mb["node"].values() if m.idiom == "led")
    assert set(led.refs()) == {"R5", "D1"} and led.extra["end"] == "up"


def test_instantiate_proves_every_wire_against_the_netlist():
    pr = _board("c3-devboard", "inst")
    sch = O.load_sheet(pr, None, create=False)
    D = Design(sch, export_netlist(pr))
    mb = IM.match_block(D, next(b for b in D.blocks if b.id == "U1"))
    (a, rail), m = next(iter(sorted(mb["decap"].items(), key=lambda t: D.text(t[0][1]))))
    pts = [(0.0, 0.0)]
    good = IMOD.instantiate(D, m, IP.decap_template(D, m, pts, (-1, 0), 4))
    assert set(good["used"]) == set(m.refs()) and len(good["powers"]) == 2
    assert all(o in ("up", "down") for _n, _q, o, _v in good["powers"])
    for k, a_, b_ in good["wires"]:
        assert a_[0] == b_[0] or a_[1] == b_[1]
    # the same geometry with the nets swapped is a short between the rail and ground: refused
    bad = IMOD.Match("decap", dict(m.roles), {"RAIL": m.nets["GND"], "GND": m.nets["RAIL"]}, m.host)
    t = IP.decap_template(D, m, pts, (-1, 0), 4)
    for mm, why in ((bad, "swapped nets"),):
        try:
            IMOD.instantiate(D, mm, t)
            raise AssertionError(f"{why}: accepted")
        except IMOD.IdiomError:
            pass
    # a supply drawn 'down' / a wire that touches a pin of another net / a diagonal wire: each refused
    t2 = IP.decap_template(D, m, pts, (-1, 0), 4)
    t2.powers = [(r, q, "down") for r, q, _o in t2.powers]
    t3 = IP.decap_template(D, m, pts, (-1, 0), 4)
    t3.wires.append(("GND", [(0.0, 0.0), (-5.08, 0.0)]))
    t4 = IP.decap_template(D, m, pts, (-1, 0), 4)
    t4.wires.append(("RAIL", [(0.0, 0.0), (-2.54, -2.54)]))
    t5 = IP.decap_template(D, m, pts, (-1, 0), 4)
    t5.wires = [w for w in t5.wires if w[0] != "GND"]
    t5.powers = [p for p in t5.powers if p[0] != "GND"]
    for tt, why in ((t2, "supply drawn down"), (t3, "foreign wire on a pin"), (t4, "diagonal"), (t5, "pin left open")):
        try:
            IMOD.instantiate(D, m, tt)
            raise AssertionError(f"{why}: accepted")
        except IMOD.IdiomError:
            pass
    # a context pin of another net under a template wire is refused as well
    try:
        IMOD.instantiate(D, m, IP.decap_template(D, m, pts, (-1, 0), 4), {(-2.54, 0.0): m.nets["GND"]})
        raise AssertionError("context pin of another net: accepted")
    except IMOD.IdiomError:
        pass


def test_templates_on_two_symbol_variants():
    """Poses come from the real pin geometry: the node and LED templates work for 2-pin parts of different
    libraries / pin lengths, in both directions, and never draw a sideways power symbol."""
    seen = 0
    for name in ("c3-devboard", "phone-breathalyzer", "stm32-sensor", "li-ion-powerbank"):
        pr = _board(name, "variants")
        D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
        for info in D.blocks:
            for k, m in IM.match_block(D, info)["node"].items():
                make = IP.led_template if m.idiom == "led" else IP.node_template
                for d in (1, -1):
                    t = None
                    for kk in range(4, 40, 2):
                        t = make(D, m, (0.0, 0.0), d, kk)
                        if t is not None:
                            break
                    assert t is not None, (name, m.idiom)
                    piece = IMOD.instantiate(D, m, t)
                    assert all(o in ("up", "down") for _n, _q, o, _v in piece["powers"]), (name, m.idiom)
                    if m.idiom == "led":
                        # anode towards the rail, drawn left to right or top to bottom
                        ledref = next(r for r in m.refs() if IM.is_led(D, r))
                        pw = D.units[ledref][0].pins_world()
                        an = next(p for p in pw if str(p.get("name", "")).upper() == "A")
                        ka = next(p for p in pw if str(p.get("name", "")).upper() == "K")
                        dx, dy = ka["xy"][0] - an["xy"][0], ka["xy"][1] - an["xy"][1]
                        assert (abs(dx) >= abs(dy) and dx > 0) or (abs(dy) > abs(dx) and dy > 0), (name, d, dx, dy)
                    seen += 1
    assert seen >= 20, seen


# ───────────────────────── what the sheets look like, in numbers ─────────────────────────
# measured on 2026-11-10 after work package 1; the limits sit just outside the measured values and guard regressions.
# NOT met from the design's wish list (reported in the work-package summary): decap_dist_mean_mm <= 10 and
# power_sideways == 0 on all seven (a supply pin between two wired pins of one side has no upright, crossing-free
# symbol position).
# Package A (power conventions, 2026-11-11): a supply / ground symbol is reached by a straight wire or ONE L, never by
# an arch that leads ground up and over or a supply down and under (12 such hooks on the seven sheets before). The
# two sideways symbols of stm32-sensor are upright now (an L across the two crystal wires). What is left lies on its
# side because no upright place exists without an arch: the lower VBUS contact of the 16-pin USB-C receptacle (ground
# below it, the CC2 / D+ / D- rows above: c3-devboard, rp2040-min) and the exposed pad of the TP4056 (four wired rows
# below it: li-ion-powerbank). Counted and reported, see bench/sch/powercheck.py.
SIDEWAYS_MAX = {"c3-devboard": 1, "li-ion-powerbank": 1, "rp2040-min": 1}
DECAP_MAX = {"c3-devboard": 28.0, "li-ion-powerbank": 19.0, "motor-driver": 30.0, "opamp-afe": 12.0,
             "phone-breathalyzer": 16.0, "rp2040-min": 39.0, "stm32-sensor": 23.0}
# (class D: rp2040-min 32.9 -> 37.3 mm. All thirteen caps of the MCU hang on the two rail bars that are wired to its
# supply pins - ten on +3V3 in two rows, three on +1V1; seven of them were three groups on supply symbols of their
# own before. The mean distance to the pins is the wrong number for that drawing: the score now judges only the caps
# that are NOT wired to their IC by their distance (decap_dist_score_mm), and decap_detached counts them.)
# (fix round 2: rp2040-min 30.6 -> 32.9 mm. The cap groups that find no room at their pins now stand in ONE row above
# the part - the critics' "irregular staircase" - and that row is 2 mm further from the pins than the staircase was.)


def test_pro2_metrics():
    cov = []
    for name in BOARDS:
        pr, r, D0, sig0 = _laid(name)
        m = r["metrics"]
        for key in ("score", "score_v1", "hard", "penalties", "labels_floor", "labels_redundant", "idiom_coverage",
                    "max_junctions_on_one_power_net", "led_against_flow", "flag_islands", "border_min_mm",
                    "staggered_pairs", "flow_violations", "connector_edge_ratio", "frame_ragged", "notes_count"):
            assert key in m, (name, key)
        assert m["idiom_coverage_kind"] == "drawn"
        assert m["overlaps"] == 0, (name, m.get("overlap_list"))
        assert m["offgrid_pins"] == 0, name
        assert m["max_junctions_on_one_power_net"] <= exemplar.MAX_POWER_JUNCTIONS, (name, m["max_junctions_on_one_power_net"])
        assert m["led_against_flow"] == 0, name
        assert m["power_sideways"] <= SIDEWAYS_MAX.get(name, 0), (name, m["power_sideways"])
        assert m["decap_dist_mean_mm"] <= DECAP_MAX[name], (name, m["decap_dist_mean_mm"])
        assert m["wire_through_text_or_body"] <= 2, (name, m["wire_through_text_or_body"])
        assert 0 <= m["score"] <= 100
        assert r["idioms"]["coverage"] == m["idiom_coverage"]
        cov.append(m["idiom_coverage"])
    assert sum(cov) / len(cov) >= 0.4, cov
    assert sum(1 for name in BOARDS if _laid(name)[1]["metrics"]["power_sideways"] == 0) >= 4
    assert sum(_laid(name)[1]["metrics"]["power_sideways"] for name in BOARDS) <= 3


def test_grounds_are_grouped_not_combed():
    """A module with many ground pins gets one ground symbol per group of at most four pins."""
    pr, r, D0, sig0 = _laid("c3-devboard")
    sch = O.load_sheet(pr, None, create=False)
    big = 0
    for g in O.geo_groups(sch):
        mine = [p for p in g["pins"] if p[0] == "U2" and D0.is_gnd(D0.view.pin_net.get(p))]
        if mine:
            xy = set()
            for s in sch.symbols(include_power=False):
                if s.ref == "U2":
                    xy |= {tuple(p["xy"]) for p in s.pins_world() if (s.ref, p["number"]) in mine}
            assert len(xy) <= 4, (len(xy), sorted(mine))
            assert len(g["power"]) == 1, (sorted(mine), len(g["power"]))
            big += 1
    assert big >= 5, big


def test_decaps_are_wired_to_the_pin_they_serve():
    """LDO input / output caps hang on the regulator's own pin wire, within 10 mm of the pin tip."""
    for name, reg in (("c3-devboard", "U1"), ("rp2040-min", "U3"), ("phone-breathalyzer", "U2")):
        pr, r, D0, sig0 = _laid(name)
        sch = O.load_sheet(pr, None, create=False)
        grp = {}
        for gi, g in enumerate(O.geo_groups(sch)):
            for p in g["pins"]:
                grp[p] = gi
        pos = {s.ref: s for s in sch.symbols(include_power=False)}
        n = 0
        for c, role in D0.role.items():
            if role["anchor"] != reg or role["kind"] != "decap" or not c.startswith("C"):
                continue
            rail = role["via"]
            cp = next(p for p, k in D0.pins_of[c].items() if k == rail)
            hp = [p for p, k in D0.pins_of[reg].items() if k == rail]
            assert any(grp.get((c, cp)) is not None and grp.get((c, cp)) == grp.get((reg, p)) for p in hp), (name, c)
            tips = [q["xy"] for q in pos[reg].pins_world() if q["number"] in hp]
            d = min(abs(pos[c].at[0] - t[0]) + abs(pos[c].at[1] - t[1]) for t in tips)
            assert d <= 10.0, (name, c, d)
            n += 1
        assert n >= 2, (name, n)


def test_score_v2_hard_zeros_and_v1_kept():
    pr = _board("li-ion-powerbank", "score")
    sch = O.load_sheet(pr, None, create=False)
    nl = export_netlist(pr)
    m = score.measure(sch, nl)
    assert m["idiom_coverage_kind"] == "matchable" and 0 < m["idiom_coverage"] <= 1
    assert set(m["penalties"]) <= {"wiring", "label_density", "idiom_coverage", "decap_distance", "satellite_distance",
                                   "flow", "connector_edges", "crossings", "bends", "staggered", "fill", "frames",
                                   "notes_title", "gnd_combs", "hard"}
    for h in m["hard"]:
        key = h.split("=")[0]
        assert key in score.HARD_ZEROS or key == "border_min_mm", h
    assert ("power_sideways=%d" % m["power_sideways"] in m["hard"]) == (m["power_sideways"] > 0)
    assert m["score"] <= 100 - 5 * len(m["hard"]) + 1e-6 or len(m["hard"]) > 6
    assert exemplar.SHEETS >= 50 and exemplar.LABELS_PER_PART["p50"] < exemplar.LABELS_PER_PART["p75"]


# ───────────────────────── work package 2: stage idioms, docking, in-block wiring ─────────────────────────
# measured on 2026-11-10 after work package 2. NOT met from the design's wish list (see the work-package summary):
#   * rp2040-min labels <= 40: 35 names are forced by the gate (G3) and the 16 GPIO nets end on a 2x10 header in
#     another frame (one more label each, by the design's own "wide bus = label column" rule): 56 measured
#   * "zero labels" on the feedback / follower / SW nets: those nets are NAMED in the source sheets, so G3 keeps
#     exactly one label each; the tests below assert "one wire group, exactly the forced names" instead
# fix round 1 (critic round after work package 4): the in / out pins of a TVS array are named on both sides instead of
# being joined by a wire ring around the part, and a net is named instead of wired around an IC's body - two more
# names on the three USB designs, three on rp2040-min
# work package 2, second pass: a pass-through TVS array is drawn with a bar over / under the part (build.Blk._bridges,
# a designed drawing - not the router's ring), so its nets are one wire with one name again: two names fewer on each
# USB design, and c3 / breathalyzer / rp2040 are back above the 0.85 target
# Fix round 2 (three critics, 6.9 / 10): the bar around a pass-through TVS array is off by default (it read as a frame
# around the part: "worst recurring geometry defect") and a wire between two multi-pin parts that would have to go
# AROUND something is two names. Both cost names and wired pin pairs on purpose; measured after that round.
# Fix round 3 (three critics, 7.3 / 10): rp2040-min 60 -> 65. The QSPI bus between flash and MCU is named completely
# instead of half wired / half named (+3), and the far contact of each tactile switch carries the net's name instead
# of a wire box around the part (+2).
# Package B (wiring idioms): motor-driver 24 -> 27 (the three lines to the programming header were three parallel
# doglegs with a name at one end only: now named at both ends), opamp-afe 14 -> 15 (VREF ran as a three-bend wire
# around the reference's capacitor: now two names).
LABELS_MAX = {"c3-devboard": 28, "li-ion-powerbank": 24, "motor-driver": 27, "opamp-afe": 15, "phone-breathalyzer": 21,
              "rp2040-min": 65, "stm32-sensor": 18}
# wired_local_ratio counts PIN PAIRS of a net inside one block, so one name on a five-pin net costs four pairs. The
# design target is 0.85; the sheets below it are the ones where fix round 1 deliberately names a net instead of
# drawing a ring around a TVS array (c3, breathalyzer, rp2040) or a loop under the MCU (stm32 NRST). Regression guards,
# measured after that round.
# Second pass of work package 2: only stm32-sensor is left below the target (NRST is named on the debug header instead of
# being wired around the MCU, and the header's SWDIO / SWCLK rows are the other way round than the MCU's).
# Fix round 2: c3 / breathalyzer / rp2040 are below the target again (D+ / D- run straight THROUGH the TVS array: one
# name on each side instead of a bar around the part), opamp-afe names the SPI lines to its header instead of crossing
# them three times.
# Fix round 3: rp2040-min 0.72 -> 0.51 (the whole QSPI bus and the switches' far contacts are names, see LABELS_MAX),
# stm32-sensor 0.68 -> 0.64 (NRST is named at the switch and at the pin instead of running around the crystal).
# Package B: a named wire between two multi-pin parts runs straight or turns once and crosses nothing
# (route.TIGHT_MAX_BENDS / TIGHT_NO_CROSS); what needed a dogleg or a crossing is two names: motor-driver 0.875 -> 0.75
# (programming header), opamp-afe 0.84 -> 0.72 (VREF). The other way round: phone-breathalyzer 0.80 -> 0.87 and
# stm32-sensor 0.68 -> 0.80 (pull-down / filter cap / reset button hang on the pin's wire instead of on a name).
WIRED_MIN = {"c3-devboard": 0.75, "li-ion-powerbank": 0.85, "motor-driver": 0.74, "opamp-afe": 0.71,
             "phone-breathalyzer": 0.85, "rp2040-min": 0.50, "stm32-sensor": 0.78}


def _p(lib, id_, value=None):
    d = {"op": "place", "lib_id": lib, "id": id_}
    if value:
        d["value"] = value
    return d


def _c(net, *pins):
    return {"op": "connect", "pins": list(pins), "net": net}


# second symbol variants: a single 5-pin op-amp as an inverting amplifier, a SOT-23-5 boost fed from a supply RAIL,
# an 8-pin buck with a bootstrap cap on its switch node
VARIANT_OPS = [
    _p("Amplifier_Operational:MCP6001-OT", "u"), _p("Device:R", "rf", "100k"), _p("Device:R", "rin", "10k"),
    _p("Device:R", "rv1", "10k"), _p("Device:R", "rv2", "10k"),
    _p("Connector_Generic:Conn_01x02", "jin", "IN"), _p("Connector_Generic:Conn_01x02", "jout", "OUT"),
    _p("Regulator_Switching:AP3012", "bst"), _p("Device:L", "l1", "10u"), _p("Device:D_Schottky", "d1", "SS14"),
    _p("Device:C", "cin", "4u7"), _p("Device:C", "cout", "10u"), _p("Device:R", "rt", "100k"), _p("Device:R", "rb", "10k"),
    _p("Connector_Generic:Conn_01x02", "jb", "VBOOST"),
    _p("Regulator_Switching:TPS5430DDA", "bk"), _p("Device:L", "l2", "15u"), _p("Device:D_Schottky", "d2", "B340"),
    _p("Device:C", "cbt", "10n"), _p("Device:C", "co2", "220u"), _p("Device:C", "cbk", "10u"),
    _p("Device:R", "rt2", "10k"), _p("Device:R", "rb2", "3k3"), _p("Connector_Generic:Conn_01x02", "jk", "V3"),
    _p("Connector_Generic:Conn_01x02", "jp", "PWR"),
    _c("+5V", "u.5", "rv1.1", "bst.5", "bst.4", "l1.1", "cin.1", "jp.1"),
    _c("GND", "u.2", "rv2.2", "jin.2", "jout.2", "bst.2", "cin.2", "cout.2", "rb.2", "jb.2", "bk.6", "bk.9", "d2.2", "co2.2",
       "rb2.2", "cbk.2", "jk.2", "jp.2"),
    _c("VREF", "u.3", "rv1.2", "rv2.1"), _c("INV", "u.4", "rf.1", "rin.2"), _c("OUT_A", "u.1", "rf.2", "jout.1"),
    _c("SIG", "rin.1", "jin.1"),
    _c("SWB", "bst.1", "l1.2", "d1.2"), _c("VBOOST", "d1.1", "cout.1", "rt.1", "jb.1"), _c("FBB", "rt.2", "rb.1", "bst.3"),
    _c("+12V", "bk.7", "cbk.1"), _c("SWK", "bk.8", "l2.1", "d2.1", "cbt.2"), _c("BOOT", "bk.1", "cbt.1"),
    _c("V3", "l2.2", "co2.1", "rt2.1", "jk.1"), _c("FBK", "rt2.2", "rb2.1", "bk.4"),
    {"op": "no_connect", "pins": ["bk.5", "bk.2", "bk.3"]},
    {"op": "pwr_flag", "net": "+5V"}, {"op": "pwr_flag", "net": "GND"}, {"op": "pwr_flag", "net": "+12V"},
]


def _variants(tag: str) -> Project:
    """The variant design, built once with the package's own sch_apply (raw label style) and copied per test."""
    src = TMP / "variants-src"
    if not src.exists():
        from copper_kicad.mcp import registry
        import copper_kicad.tools  # noqa: F401
        src.mkdir(parents=True)
        pr = Project(src, "variants")
        out = registry.call_tool("sch_apply", {"project": str(pr.pro), "ops": VARIANT_OPS, "preview": False, "layout": "keep"})
        assert not out.get("isError"), out["content"][0]["text"][:1500]
        shutil.rmtree(src / ".copper", ignore_errors=True)
    d = TMP / f"variants-{tag}"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(src, d)
    return Project(d, "variants")


def _laid_variants():
    if "variants" not in _CACHE:
        pr = _variants("pro2")
        r = engine.relayout(pr, force=True, style="pro2")
        _CACHE["variants"] = (pr, r)
    return _CACHE["variants"]


def _after(pr):
    """(sheet, Design, {(ref, pin): geometric group}, {(ref, pin): xy}, stage matches) of a laid-out project."""
    sch = O.load_sheet(pr, None, create=False)
    D = Design(sch, export_netlist(pr))
    grp, xy = {}, {}
    groups = O.geo_groups(sch)
    for gi, g in enumerate(groups):
        for p in g["pins"]:
            grp[p] = gi
    for s in sch.symbols(include_power=False):
        for p in s.pins_world():
            xy[(s.ref, p["number"])] = tuple(p["xy"])
    ms = [m for info in D.blocks for m in ST.all_matches(ST.match_block(D, info, set()))]
    return sch, D, grp, xy, ms, groups


def _one_piece(D, grp, k) -> bool:
    return len({grp.get(p) for p in D.view.nets[k].pins}) == 1


def _names(sch, D, k) -> int:
    return sum(1 for l in sch.labels() if l["net"] == D.text(k))


def _pin_on(D, ref, k):
    return next(p for p, n in sorted(D.pins_of[ref].items()) if n == k)


def test_wp2_bench_numbers():
    stag = 0
    for name in BOARDS:
        m = _laid(name)[1]["metrics"]
        assert m["wired_local_ratio"] >= WIRED_MIN[name], (name, m["wired_local_ratio"])
        assert m["labels_redundant"] == 0, name
        assert m["wire_through_text_or_body"] == 0, (name, m["wire_through_text_or_body"])
        assert m["crossing_near_junction"] == 0, (name, m["crossing_near_junction"])
        assert m["labels"] <= LABELS_MAX[name], (name, m["labels"])
        assert m["labels"] >= m["labels_floor"], name             # the gate (G3) forces one label per name
        stag += m["staggered_pairs"] == 0
    assert stag >= 5, stag


def _tvs_nets(D):
    """[(part, net, [its two pins])] for every small part with one signal net on two of its pins (TVS in / out)."""
    out = []
    for ref in sorted(D.units):
        if D.is_conn(ref) or not (5 <= D.npins(ref) <= 8):
            continue
        by = {}
        for p, k in D.pins_of[ref].items():
            if D.conn(k) and not D.is_rail(k):
                by.setdefault(k, []).append(p)
        face = {p["number"]: tuple(p["out"]) for p in D.units[ref][0].pins_world()}
        out += [(ref, k, ps) for k, ps in sorted(by.items(), key=str)
                if len(ps) == 2 and {face[ps[0]], face[ps[1]]} == {(-1, 0), (1, 0)}]
    return out


def test_pass_through_tvs_is_bridged_by_a_bar():
    """The in / out pins of a TVS array (USBLC6 on three corpus designs, behind two different USB-C symbols).
    Default since fix round 2: the data line runs straight THROUGH the part - a wire on the connector side, the name
    on the other - and no wire goes around it. With build.PASS_THROUGH_BAR on: one wire group and ONE name per data
    line, the part's ground pin pointing down with its symbol straight below it, the supply pin up."""
    seen = 0
    for name in ("c3-devboard", "rp2040-min", "phone-breathalyzer"):
        pr, r, _D0, _sig = _laid(name)
        assert r["style_used"] == "pro2", (name, r.get("tried"))
        sch, D, grp, xy, _ms, _groups = _after(pr)
        for ref, k, ps in _tvs_nets(D):
            seen += 1
            assert grp[(ref, ps[0])] != grp[(ref, ps[1])], (name, ref, ps)        # no wire around the part
            assert _names(sch, D, k) >= 2, (name, ref, D.text(k))                 # ... both sides carry the name
        m = r["metrics"]
        assert m["crossing_near_junction"] == 0 and m["wire_through_text_or_body"] == 0 and m["overlaps"] == 0, (name, m["hard"])
    assert seen >= 6, seen
    B.PASS_THROUGH_BAR = True
    try:
        seen = 0
        for name in ("c3-devboard", "rp2040-min", "phone-breathalyzer"):
            pr = _board(name, "bar")
            r = engine.relayout(pr, force=True, style="pro2")
            assert r["style_used"] == "pro2", (name, r.get("tried"))
            sch, D, grp, xy, _ms, _groups = _after(pr)
            pw = [(s.value, tuple(round(v, 2) for v in s.pins_world()[0]["xy"])) for s in sch.symbols() if s.is_power]
            for ref, k, ps in _tvs_nets(D):
                seen += 1
                assert grp[(ref, ps[0])] == grp[(ref, ps[1])], (name, ref, ps)
                local = [p for p in D.view.nets[k].pins if D.block_of.get(p[0]) == D.block_of.get(ref)]
                assert len({grp[p] for p in local}) == 1, (name, ref, D.text(k))
                si = D.units[ref][0]
                for p in si.pins_world():
                    kk = D.pins_of[ref].get(p["number"])
                    if D.is_rail(kk) and p["out"][0] == 0:
                        assert (p["out"][1] > 0) == D.is_gnd(kk), (name, ref, p["number"])   # ground down, supply up
                        assert any(v == D.text(kk) and abs(q[0] - round(p["xy"][0], 2)) < 0.01
                                   and 0 < (q[1] - p["xy"][1]) * p["out"][1] <= 12.7 for v, q in pw), (name, ref, p["number"])
            m = r["metrics"]
            assert m["crossing_near_junction"] == 0 and m["wire_through_text_or_body"] == 0 and m["overlaps"] == 0, (name, m["hard"])
            if name == "c3-devboard":
                assert m["labels"] == _laid("c3-devboard")[1]["metrics"]["labels"] - 2
        assert seen >= 6, seen
    finally:
        B.PASS_THROUGH_BAR = False


def test_doubled_pin_pair_offers_both_rows():
    """A doubled pair (USB-C D+ on A6 / B6) records its other row as an alternative wire end: beyond the jumper of
    the pair it is interleaved with, and never on a point another net's wire ends on."""
    sch, D, _sig = _memory_layout("c3-devboard", "alts")
    info = next(i for i in D.blocks if any(D.is_conn(a) and D.npins(a) >= 12 for a in i.anchors))
    blk = B.Blk(D, info)
    blk.enable_v2()
    j = next(a for a in info.anchors if D.is_conn(a))
    blk.sm = None
    blk.place(D.units[j][0], (0.0, 0.0))
    assert len(blk.alts) == 2, blk.alts
    ends = {}
    for n, a, b in blk.wires:
        for e in (a, b):
            ends.setdefault((round(e[0], 2), round(e[1], 2)), set()).add(n)
    for n, first, tip, ws, o in blk.alts:
        assert any(t[0] == n and t[1] == first for t in blk.terms), (n, first)
        assert tip[1] != first[1] and (tip[0] - first[0]) * o[0] >= -0.01, (first, tip)
        for _n, a, b in ws:
            for e in (a, b):
                assert ends.get((round(e[0], 2), round(e[1], 2)), {n}) <= {n}, (n, e)


def test_opamp_feedback_is_a_loop_above_the_body():
    """Both units of the LM358 and the single MCP6001: inverting input on top, feedback parts above the body,
    feedback / follower net wired in ONE piece with exactly the names the source sheet forces (G3)."""
    seen = {"follower": 0, "gain": 0}
    for pr in (_laid("opamp-afe")[0], _laid_variants()[0]):
        sch, D, grp, xy, ms, _g = _after(pr)
        ops_ = [m for m in ms if m.idiom == "opamp"]
        assert ops_, "no op-amp stage matched"
        for m in ops_:
            ref, ui = m.host
            e = m.extra
            assert xy[(ref, e["inv"])][1] < xy[(ref, e["ni"])][1], (ref, ui, "inverting input is not the upper pin")
            assert xy[(ref, e["out"])][0] > xy[(ref, e["inv"])][0]
            k = m.nets["INV"]
            assert _one_piece(D, grp, k) or len({D.block_of.get(p[0]) for p in D.view.nets[k].pins}) > 1, (ref, D.text(k))
            assert grp[(ref, e["inv"])] == grp[(ref, e["out"])] if e["follower"] else True
            inblock = [p for p in D.view.nets[k].pins if D.block_of.get(p[0]) == D.block_of.get(ref)]
            assert len({grp[p] for p in inblock}) == 1, (ref, D.text(k), "feedback net is not wired inside its block")
            if not e["follower"]:
                assert _names(sch, D, k) == len(D.view.nets[k].names), (D.text(k), _names(sch, D, k))
            top = min(b[1] for s in D.units[ref] if s.unit == D.units[ref][ui].unit for b in [s.body_bbox()])
            for role, r in m.roles.items():
                if role.startswith("fb"):
                    assert all(xy[(r, p)][1] < top for p in D.pins_of[r]), (r, "feedback part is not above the body")
                    assert len({xy[(r, p)][1] for p in D.pins_of[r]}) == 1, (r, "feedback part is not horizontal")
            seen["follower" if e["follower"] else "gain"] += 1
    assert seen["follower"] >= 1 and seen["gain"] >= 2, seen


def test_boost_reads_left_to_right_and_buck_runs_off_the_switch_pin():
    kinds = {"boost": 0, "buck": 0}
    for pr in (_laid("li-ion-powerbank")[0], _laid("motor-driver")[0], _laid_variants()[0]):
        sch, D, grp, xy, ms, _g = _after(pr)
        for m in ms:
            if m.idiom not in kinds:
                continue
            L, sw, vout = m.roles["L"], m.nets["SW"], m.nets["VOUT"]
            assert _one_piece(D, grp, sw), (m.idiom, "the switch node is not wired in one piece")
            assert _names(sch, D, sw) == len(D.view.nets[sw].names), (D.text(sw), "more labels than the forced names")
            if m.idiom == "boost":
                d = m.roles["D"]
                xs = [xy[(L, _pin_on(D, L, m.nets["VIN"]))][0], xy[(L, _pin_on(D, L, sw))][0],
                      xy[(d, _pin_on(D, d, sw))][0], xy[(d, _pin_on(D, d, vout))][0]]
                assert xs[0] < xs[1] < xs[2] < xs[3], (m.host, xs)       # VIN < L < SW node < D < VOUT
                ys = {xy[(L, p)][1] for p in D.pins_of[L]} | {xy[(d, p)][1] for p in D.pins_of[d]}
                assert len(ys) == 1, (m.host, "inductor and diode are not on one line")
                for role, r in m.roles.items():
                    if role.startswith("cin"):
                        assert xy[(r, _pin_on(D, r, m.nets["VIN"]))][0] < xs[0]
                    if role.startswith("cout") or role == "rt":
                        assert xy[(r, _pin_on(D, r, vout))][0] > xs[3]
            else:
                hx = xy[m.host][0]
                assert hx < xy[(L, _pin_on(D, L, sw))][0] < xy[(L, _pin_on(D, L, vout))][0], m.host
                assert xy[m.host][1] == xy[(L, _pin_on(D, L, sw))][1], (m.host, "the inductor is not on the SW pin's row")
            if "rt" in m.roles:
                fb = m.nets["FB"]
                assert _one_piece(D, grp, fb), (m.idiom, "feedback tap is not wired to the FB pin")
            kinds[m.idiom] += 1
    assert kinds["boost"] >= 2 and kinds["buck"] >= 2, kinds


def test_crystal_is_wired_with_one_shared_ground():
    n = 0
    for name in ("rp2040-min", "stm32-sensor"):                    # a 4-pin crystal behind a series R; a 2-pin one
        sch, D, grp, xy, ms, groups = _after(_laid(name)[0])
        for m in [m for m in ms if m.idiom == "crystal"]:
            y = m.roles["Y"]
            for tag in "AB":
                assert _one_piece(D, grp, m.nets["N" + tag]), (name, D.text(m.nets["N" + tag]))
                assert _one_piece(D, grp, m.nets["H" + tag]), (name, D.text(m.nets["H" + tag]))
            # the two load caps share ONE ground bar with ONE symbol; a case pin of a 4-pin crystal is on that bar
            # too, or (fix round 3: the pin that lies above its side's signal pin) has a ground symbol of its own -
            # taken down to the bar it closed a ring of wire around the whole crystal
            gp = {(r, p) for role, r in m.roles.items() if role in ("CA", "CB")
                  for p, k in D.pins_of[r].items() if D.is_gnd(k)}
            gs = {grp[p] for p in gp}
            assert len(gs) == 1, (name, "the load caps are not on one ground bar")
            assert len(groups[next(iter(gs))]["power"]) == 1, (name, "more than one ground symbol")
            case = {grp[(m.roles["Y"], p)] for p, k in D.pins_of[m.roles["Y"]].items() if D.is_gnd(k)}
            assert len(case - gs) <= 1, (name, "more than one case pin with a ground symbol of its own")
            for g in case - gs:
                assert len(groups[g]["power"]) == 1 and len(groups[g]["pins"]) == 1, (name, groups[g])
            n += 1
    assert n == 2, n


def test_stage_templates_on_two_symbol_variants():
    """Every stage template is proven by instantiate() on (at least) two different symbols, straight from the
    template functions: nothing depends on one library part's pin layout."""
    proved: dict = {}

    def prove(kind, D, m, make):
        for args in make:
            try:
                piece = IMOD.instantiate(D, m, args())
            except IMOD.IdiomError:
                continue
            assert all(o in ("up", "down") for _n, _q, o, _v in piece["powers"]), kind
            proved.setdefault(kind, set()).add(tuple(sorted(D.comps[r]["lib_id"] for r in m.refs()))
                                               + (D.comps[m.host[0]]["lib_id"],))
            return piece
        raise AssertionError((kind, m.host, "no template variant could be proven"))
    for name in ("opamp-afe", "li-ion-powerbank", "motor-driver", "rp2040-min", "stm32-sensor", "c3-devboard", "variants"):
        pr = _variants("tmpl") if name == "variants" else _board(name, "tmpl")
        D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
        for info in D.blocks:
            sm = ST.match_block(D, info, set())
            for m in ST.all_matches(sm):
                if m.idiom in ("boost", "buck"):
                    piece = prove(m.idiom, D, m, [lambda: ST.chain_template(D, m)])
                    assert any(n == m.nets["SW"] for n, _q, _o in piece["terms"]), "no SW port"
                elif m.idiom == "crystal":
                    for s in (1, -1):
                        piece = prove("crystal", D, m, [lambda k=k, e=e, mir=mir, s=s: ST.crystal_template(
                            D, m, {"A": (0.0, 0.0), "B": (0.0, 10.16)}, s, k, e * B.U, mir)
                            for k in (6, 8, 10) for e in (0, 2, 4, 6, 8, 10) for mir in (None, "x")])
                        assert sum(1 for n, _q, _o, _v in piece["powers"] if D.is_gnd(n)) <= 2     # (shared bar + one case pin)
                elif m.idiom == "usbc":
                    for s in (1, -1):
                        for dy in (2.54, 15.24):
                            piece = prove("usbc", D, m, [lambda k=k, st=st, s=s, dy=dy: ST.cc_template(
                                D, m, {"0": (0.0, 0.0), "1": (0.0, dy)}, s, k, st) for st in (False, True) for k in (0, 2, 4, 8)])
                            assert len(piece["powers"]) == 1, "CC pull-downs do not share one ground symbol"
                elif m.idiom == "ldo":
                    piece = prove("ldo", D, m, [lambda k=k: ST.ldo_template(D, m, (0.0, 0.0), (15.24, 0.0), k) for k in (4, 6, 8)])
                    gy = {q[1] for n, q, o, _v in piece["powers"] if o == "down"}
                    assert len(gy) <= 1, "LDO grounds are not on one row"
                elif m.idiom == "opamp":
                    si = D.units[m.host[0]][m.host[1]]
                    e = m.extra
                    pose = ST.pose_lr(si, [e["inv"], e["ni"]], [e["out"]], upper=e["inv"], lower=e["ni"])
                    assert pose is not None, (name, m.host)
                    B.set_pose(si, 0.0, 0.0, pose[0], pose[1])
                    ps = B.pins(si)
                    ctx = {tuple(p["xy"]): D.pins_of[si.ref].get(no) for no, p in ps.items()}
                    top = min(b[1] for b in B.sym_boxes(si))
                    prove("opamp", D, m, [lambda k=k: ST.opamp_template(D, m, tuple(ps[e["out"]]["xy"]), tuple(ps[e["inv"]]["xy"]),
                                                                        k, top) for k in (4, 6, 8, 10, 12)])
    for kind in ("opamp", "boost", "buck", "crystal", "usbc", "ldo"):
        assert len(proved.get(kind, ())) >= 2, (kind, proved.get(kind))


def test_transistor_pose_and_peer_facing():
    n = 0
    for name in ("phone-breathalyzer", "stm32-sensor"):           # two different discrete FET symbols
        pr = _board(name, "tsw")
        D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
        for ref in sorted(D.units):
            tp = ST.transistor_pose(D, ref)
            if tp is None:
                continue
            si = D.units[ref][0]
            B.set_pose(si, 0.0, 0.0, tp[0], tp[1])
            names = {str(p.get("name", "")).upper(): tuple(p["out"]) for p in si.pins_world()}
            ctrl = next(o for nm, o in names.items() if nm in ("G", "B", "GATE", "BASE"))
            low = next(o for nm, o in names.items() if nm in ("S", "E", "SOURCE", "EMITTER"))
            assert ctrl[1] == 0 and low[0] == 0, (name, ref, names)
            n += 1
        assert "tswitch" in _laid(name)[1]["idioms"]["matches"], name
    assert n >= 2, n
    # peer IC: the flash faces the MCU. Fix round 3: a bus is drawn ONE way - only three of the six QSPI nets can be
    # wired straight across (the other flash pins look away), so the whole bus is named at both ends ...
    from copper_kicad.sch.pro import dock as DOCK

    def qspi(grp, xy, D):
        rows = [k for k in D.view.nets if D.text(k).startswith("QSPI") and any(
            xy[a][1] == xy[b][1] and grp[a] == grp[b] for a in D.view.nets[k].pins for b in D.view.nets[k].pins
            if a[0] == "U1" and b[0] == "U2")]
        wired = [k for k in D.view.nets if D.text(k).startswith("QSPI") and any(
            grp[a] == grp[b] for a in D.view.nets[k].pins for b in D.view.nets[k].pins if a[0] == "U1" and b[0] == "U2")]
        return rows, wired
    sch, D, grp, xy, _ms, _g = _after(_laid("rp2040-min")[0])
    rows, wired = qspi(grp, xy, D)
    # (class L, bundle.py) ... until the pair is planned as ONE bundle: the flash pins that look away are led around
    # the flash on nested lanes, all six nets are wires (test_class_l_flash_and_headers_are_wired_as_bundles)
    assert len(wired) == 6, wired
    assert route.bus_prefix("QSPI_SD0") == route.bus_prefix("QSPI_SS") == "QSPI" and route.bus_prefix("NRST") == ""
    # ... and with the rule off the facing placement still wires three of them, two pin row on pin row
    # (package B: the BOOTSEL button now hangs on its wire in front of the QSPI rows; this comparison is about the
    # facing placement alone, so it is made the way the sheet was drawn then)
    # (class I: ... and with the crystal where the first fit put it then - the drawing with the shortest wires
    # stands nearer the pins, in the room this old facing placement used)
    assert DOCK.BUS_WHOLE and engine.BUS_ALL_OR_NONE and IM.TWIN_CONTACTS and DOCK.CRYSTAL_BEST
    DOCK.BUS_WHOLE = engine.BUS_ALL_OR_NONE = IM.TWIN_CONTACTS = DOCK.CRYSTAL_BEST = False
    try:
        sch2, D2, _sig = _memory_layout("rp2040-min", "peerbus")
    finally:
        DOCK.BUS_WHOLE = engine.BUS_ALL_OR_NONE = IM.TWIN_CONTACTS = DOCK.CRYSTAL_BEST = True
    grp2, xy2 = {}, {}
    for gi, g in enumerate(O.geo_groups(sch2)):
        for p in g["pins"]:
            grp2[p] = gi
    for s_ in sch2.symbols(include_power=False):
        for p in s_.pins_world():
            xy2[(s_.ref, p["number"])] = tuple(p["xy"])
    rows, wired = qspi(grp2, xy2, D2)
    assert len(rows) >= 2 and len(wired) >= 3, (rows, wired)     # (the third bus wire lands on a pull-up's stem end)


def test_router_pro2_rules_and_packing():
    """+20 per crossing / +8 per bend, crossings stay 2.54 mm away from wire ends, the bend / crossing limits turn
    a route down; pack_1d keeps order without overlap and leaves a fitting item on its row."""
    U_ = route.U
    R = route.Router([], [], {}, [("A", (0.0, 0.0), (20 * U_, 0.0))], (-40, -40, 60, 40))
    R.v2 = True
    segs = R.route("B", {(10, -4)}, {(10, 4)}, 200, 3, 1)
    assert segs is not None and len(segs) == 1, segs                # straight across, far from both wire ends
    R = route.Router([], [], {}, [("A", (0.0, 0.0), (20 * U_, 0.0))], (-40, -40, 60, 40))
    R.v2 = True
    segs = R.route("B", {(1, -4)}, {(1, 4)}, 200, 6, 1)
    assert segs is not None
    cells = {c for _n, a, b in segs for c in route.seg_cells(a, b)}
    assert not any(c in cells for c in ((0, 0), (1, 0), (19, 0), (20, 0))), "crossed next to a wire end"
    R = route.Router([], [], {}, [("A", (0.0, 0.0), (20 * U_, 0.0))], (-40, -40, 60, 40))
    R.v2 = True
    assert R.route("B", {(1, -4)}, {(1, 4)}, 200, 0, 1) is None      # would need bends: refused, a label instead
    assert R.route("B", {(10, -4)}, {(10, 4)}, 200, 3, 0) is None    # would need a crossing: refused
    assert R.stats["rejected"] >= 1
    pos = DOCK.pack_1d([0.0, 1.0, 10.0], [-2.0, -2.0, -2.0], [2.0, 2.0, 2.0], 0.0)
    assert pos[0] < pos[1] < pos[2] and pos[1] - pos[0] >= 4.0 - 1e-6 and abs(pos[2] - 10.0) < 1e-6, pos
    assert abs((pos[0] + pos[1]) / 2 - 0.5) < 1e-6, pos             # the two that collide share the displacement


def test_block_falls_back_to_the_legacy_build():
    """A bug in the docking layer costs that block its stage idioms, not the sheet and not the pro2 rung."""
    pr = _board("opamp-afe", "dockbug")
    orig = DOCK.build_block

    def boom(blk, origin=(0.0, 0.0)):
        raise RuntimeError("docking broken on purpose")
    DOCK.build_block = boom
    try:
        r = engine.relayout(pr, force=True, style="pro2")
    finally:
        DOCK.build_block = orig
    assert r["style_used"] == "pro2", (r["style_used"], r.get("tried"))
    assert "identical" in r["connectivity"]
    assert any(d.startswith("block:") for d in r["idioms"]["dropped"]), r["idioms"]
    assert "opamp" not in r["idioms"]["matches"]


def test_variant_design_is_laid_out_in_pro2():
    pr, r = _laid_variants()
    assert r["style_used"] == "pro2", (r["style_used"], r.get("tried"))
    assert "identical" in r["connectivity"]
    for k in ("opamp", "boost", "buck"):
        assert k in r["idioms"]["matches"], r["idioms"]
    assert r["metrics"]["overlaps"] == 0 and r["metrics"]["wire_through_text_or_body"] == 0, r["metrics"].get("overlap_list")


# ───────────────────────── work package 3: flow ranks, page composition, cross-block wiring ─────────────────────────
from copper_kicad.sch.pro import flow as FLOW  # noqa: E402
from copper_kicad.sch.pro import page as PAGE  # noqa: E402

UNSEEN = ROOT / "bench" / "sch" / "corpus_unseen"
WP2_CROSSINGS = {"c3-devboard": 6, "li-ion-powerbank": 0, "motor-driver": 0, "opamp-afe": 3, "phone-breathalyzer": 3,
                 "rp2040-min": 7, "stm32-sensor": 2}
PA_CROSSINGS = {"li-ion-powerbank": 6, "stm32-sensor": 3,     # package A, see test_wp3_bench_numbers
                # class L (bundle.py): the QSPI flash is wired to the MCU. Its pin order against the MCU's leaves five
                # inverted pairs - five square crossings that no drawing of these two symbols can avoid - and the
                # branch to the boot switch may cross one wire: 6 -> 12
                "rp2040-min": 12,
                # fix round 7: the TVS stands at the input, the LED on the buck's line - the frames fit A4 WITH the
                # programming header wired (4 wires, 3 crossings = the floor of its pin order plus the ground lead)
                "motor-driver": 3}
# measured at the end of work package 3 (regression guards; the design targets that are NOT met are 0.60 for every
# sheet and labels_per_signal_net <= 1.0, see the report of the package)
# fix round 1: a frame is as large as its drawing (frames are no longer stretched over the sheet, which the critics
# read as "oversized, mostly empty frames"), so the drawing's bounding box covers less of the sheet where the paper
# is larger than the circuit needs (c3, breathalyzer, rp2040). Measured after that round.
# fix round 4: rp2040-min stands in three rank columns across its A3 sheet (USB-C and LDO left, MCU in the middle,
# GPIO header at the right edge, the debug header undocked to the left edge) instead of the MCU under a row of small
# frames with the right third of the sheet empty; the outline of that band covers 0.45 of the sheet (was 0.47+).
# package D: every frame hugs its drawing (measured, then composed again - engine._refit) and no column is
# stretched any more, so the outline of the ink covers less of a sheet whose paper is larger than the circuit needs.
# Measured after that package: c3 0.425, li-ion 0.627, motor 0.547, opamp 0.559, breathalyzer 0.422, rp2040 0.425,
# stm32 0.577 (the floors sit just under; test_package_d_* hold the rules that replaced the stretched frames).
# (fix round 6: c3-devboard 0.425 -> 0.407. Its USB-C frame is 7.6 mm narrower - the Reference / Value texts of a block
# now count for its frame wherever they stand, and the second drawing hugs them - so the frames' outline covers less;
# opamp-afe 0.559 -> 0.501: the converter's frame lost the strip that held the PWR_FLAG of VREF (the flag stands on the
# routed wire now) and the amplifier's frame the room of a name that became a wire; smaller frames, same A4 sheet.)
# (class P) moved up with the page rules of class P (channel slack, the outline's proportions, a column over the title
# block is not "short"): measured c3 0.455, li-ion 0.641, motor 0.573, opamp 0.571, breathalyzer 0.437, rp2040 0.506,
# stm32 0.585 (were 0.417 / 0.641 / 0.547 / 0.556 / 0.422 / 0.454 / 0.561)
FILL_MIN = {"c3-devboard": 0.44, "li-ion-powerbank": 0.62, "motor-driver": 0.46, "opamp-afe": 0.55,   # (motor-driver, fix round 7: see P_FILL)
            "phone-breathalyzer": 0.42, "rp2040-min": 0.49, "stm32-sensor": 0.56}


def _frames(pr):
    sch = O.load_sheet(pr, None, create=False)
    return Design(sch, export_netlist(pr))._frames(), sch


def test_wp3_bench_numbers():
    full = 0
    for name in BOARDS:
        pr, r, D0, _sig = _laid(name)
        m = r["metrics"]
        assert r["style_used"] == "pro2", (name, r.get("tried"))
        assert m["flow_violations"] == 0, (name, m["flow_violations"])
        assert m["connector_edge_ratio"] == 1.0, (name, m["connector_edge_ratio"])
        # frame_ragged (frames of a column sharing one width) is no longer zero by design: narrow frames keep their
        # own width and may share a row - test_frames_stand_on_a_column_grid holds the rule that replaced it
        assert m["border_min_mm"] >= 10.0, (name, m["border_min_mm"])
        # a column that ends at the title block keeps the (unstretched) frames a little above the middle
        assert m["off_centre"] <= 0.07, (name, m["off_centre"])
        assert m["flag_islands"] == 0, (name, m["flag_islands"])
        # work package 4 adds ONE deliberate crossing on li-ion-powerbank: the charger's ground symbol hangs upright
        # from an L across the neighbouring pin's wire instead of lying on its side (test_upright_power_by_crossing...)
        # package A: a hook to a supply / ground symbol became an L that crosses a neighbour's wire squarely, and the
        # doubled supply / ground pins of one side share a bar that crosses the rows between them. li-ion-powerbank:
        # the VBUS bar of the 6-pin USB-C receptacle crosses CC1 / CC2, its top ground crosses the VBUS rise, and at
        # the TP4056 the TEMP - GND bar crosses PROG, the ground drop crosses VCC and the VCC rise (which now carries
        # the input cap C1) crosses PROG. stm32-sensor: VDDA rises across the two crystal wires.
        assert m["wire_crossings"] <= PA_CROSSINGS.get(name, WP2_CROSSINGS[name]), (name, m["wire_crossings"])
        assert m["sheet_fill"] >= FILL_MIN[name], (name, m["sheet_fill"])
        full += m["sheet_fill"] >= 0.60
        assert r["page"]["flow_violations"] == 0, name
        # PWR_FLAG count per net is what the sheet had (the pre-gate checks it; here against the original Design)
        sch1 = O.load_sheet(pr, None, create=False)
        assert not route.verify_partition(sch1, D0), name
    assert full >= 1, full                               # (package D: was 2 with stretched frames)


def test_frames_stand_on_a_column_grid():
    """Frames on the 2.54 mm grid, no two frames overlap, every frame lies within its column, a frame is as wide as
    its column or clearly narrower (then it keeps its own width and may share a row), each row of a column touches
    the column's left or right edge, columns do not overlap, nothing in the title-block corner, and every block of
    the report is in exactly one column."""
    for name in BOARDS:
        pr, r, _D0, _sig = _laid(name)
        fr, sch = _frames(pr)
        cols = r["page"]["columns"]
        assert sorted(i for c in cols for i in c) == sorted(fr), (name, cols, sorted(fr))
        (ax0, ay0, ax1, ay1), tb = PAGE.area(PAGE.paper_of(sch))
        for i, f in fr.items():
            for v_ in f:
                assert abs(v_ / 2.54 - round(v_ / 2.54)) < 1e-3, (name, i, f)
            assert f[0] >= ax0 - 0.01 and f[1] >= ay0 - 0.01 and f[2] <= ax1 + 0.01 and f[3] <= ay1 + 0.01, (name, i, f)
            assert not (f[2] > tb[0] + 0.01 and f[3] > tb[1] + 0.01), (name, i, "frame in the title-block corner")
        xs = []
        for c in cols:
            x0, x1 = min(fr[i][0] for i in c), max(fr[i][2] for i in c)
            for i in c:
                w = fr[i][2] - fr[i][0]
                # (package D) as wide as the column, or its own width: then clearly narrower (PAGE.EVEN)
                assert abs(w - (x1 - x0)) < 0.01 or w < PAGE.EVEN * (x1 - x0) + 0.01, (name, i, w, x1 - x0)
            tops = sorted({round(fr[i][1], 2) for i in c})
            for t in tops:                                   # one row: side by side, on one of the column's edges
                row = sorted((fr[i] for i in c if round(fr[i][1], 2) == t), key=lambda f: f[0])
                assert all(a[2] < b[0] for a, b in zip(row, row[1:])), (name, c, t)
                assert abs(row[0][0] - x0) < 0.01 or abs(row[-1][2] - x1) < 0.01, (name, c, t)
            bots = [max(fr[i][3] for i in c if round(fr[i][1], 2) == t) for t in tops]
            assert all(b <= t + 0.01 for b, t in zip(bots, tops[1:])), (name, c)
            xs.append((x0, x1))
        assert all(a[1] < b[0] for a, b in zip(xs, xs[1:])), (name, xs)
        # (fix round 4: an outer column of ONE connector frame may stand level with the frame it is wired to)
        # (package D: any column of ONE frame - it goes up or down with its drawing until its terminals stand on the
        # rows they are wired to, inside the outline of the other columns)
        solo = [len(c) == 1 for ci, c in enumerate(cols)]
        top_ = min(f[1] for f in fr.values())
        bot_ = max(f[3] for f in fr.values())
        for c, s_ in zip(cols, solo):
            if s_:
                assert top_ - 0.01 <= fr[c[0]][1] and fr[c[0]][3] <= bot_ + 0.01, (name, c)
        assert len({round(min(fr[i][1] for i in c), 2) for c, s_ in zip(cols, solo) if not s_} or {0}) == 1,             (name, "columns share the top line")


def test_flow_ranks_pin_connectors_and_break_feedback():
    pr = _board("opamp-afe", "flow")
    D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
    F = FLOW.analyse(D)
    kinds = {b.id: b.kind for b in D.blocks}
    for i, k in kinds.items():
        if k == "input":
            assert F.pin[i] == "first" and F.rank[i] == 0, (i, F.pin[i], F.rank[i])
    amp = next(b.id for b in D.blocks if "AMPLIFIER" in b.title)
    adc = next(b.id for b in D.blocks if "ADC" in b.title)
    sens = next(b.id for b in D.blocks if "SENSOR" in b.title)
    assert F.pin[sens] == "first", F.pin                      # a signal source stands at the left edge
    assert F.rank[sens] < F.rank[amp] < F.rank[adc], F.rank
    assert F.edges[(amp, adc)]["kind"] == "signal" and not F.edges[(amp, adc)]["feedback"]
    assert set(F.order) == set(kinds) and F.order.index(amp) < F.order.index(adc)
    roles = F.roles
    gnd = next(k for k in D.view.nets if D.is_gnd(k))
    assert roles[gnd]["role"] == "gnd"
    out = next(k for k in D.view.nets if D.text(k) == "AMP_OUT")
    assert roles[out]["role"] == "signal" and roles[out]["driver"] is not None and roles[out]["driver"][0].startswith("U")
    # a cycle between blocks: the weaker edge is tagged feedback and the ranks stay finite
    D.flow = {("A", "B"): 3, ("B", "C"): 2, ("C", "A"): 1}
    import types
    D.blocks = [types.SimpleNamespace(id=i, kind="ic", rank=3, stage=3.0, anchors=[], sats=[], title=i) for i in "ABC"]
    D.reg_in, D.reg_out, D.feed_of = {}, {}, {}
    F2 = FLOW.analyse(D)
    assert F2.edges[("C", "A")]["feedback"] and not F2.edges[("A", "B")]["feedback"], F2.edges
    assert F2.rank["A"] < F2.rank["B"] < F2.rank["C"], F2.rank
    assert FLOW.violations(F2, {"A": 0, "B": 1, "C": 2}) == 0 and FLOW.violations(F2, {"A": 1, "B": 0, "C": 2}) == 1


class _FakeBlk:
    def __init__(self, i):
        import types
        self.info = types.SimpleNamespace(id=i)


def _fake_page(sizes, rank, pin, edges=(), edge_blocks=(), docked=None, shared=None, npins=4):
    import types
    blocks = [_FakeBlk(i) for i in sizes]
    F = FLOW.Flow()
    F.rank, F.pin, F.order = dict(rank), dict(pin), sorted(sizes, key=lambda i: (rank[i], i))
    F.edges = {e: {"w": 1.0, "kind": "signal", "feedback": False} for e in edges}
    F.edge_blocks = set(edge_blocks)
    F.shared = shared or {}
    nets = {k: types.SimpleNamespace(pins=[0] * 2) for ks in F.shared.values() for k in ks}
    D = types.SimpleNamespace(view=types.SimpleNamespace(nets=nets), docked=docked or {}, npins=lambda r: npins,
                              block_of={c: b for c, b in (docked or {}).items()})
    sz = {id(b): sizes[b.info.id] for b in blocks}
    return blocks, sz, F, D


def test_page_compose_rules():
    """Smallest paper, pinned columns, no signal right to left, channel sized by the nets across it, undocking."""
    sizes = {"J1": (40.64, 30.48), "U1": (60.96, 50.8), "U2": (81.28, 76.2), "J2": (40.64, 40.64)}
    rank = {"J1": 0, "U1": 1, "U2": 2, "J2": 3}
    pin = {"J1": "first", "U1": None, "U2": None, "J2": "last"}
    blocks, sz, F, D = _fake_page(sizes, rank, pin, edges=[("U1", "U2")], edge_blocks=["J1", "J2"],
                                  shared={frozenset(("U2", "J2")): ["n1", "n2", "n3"]})
    pp, pos, info = PAGE.compose(blocks, sz, F, D)
    assert pp == ("B5" if PAGE.B_SERIES else "A4"), pp    # (fix round 7: the ISO B sizes stand between the A sizes)
    col = info["col_of"]
    assert col["J1"] == 0 and col["J2"] == max(col.values()), col
    assert col["U1"] <= col["U2"], col                                  # the signal edge is never drawn backwards
    by = {b.info.id: pos[id(b)] for b in blocks}
    (ax0, ay0, ax1, ay1), tb = PAGE.area(pp)
    for i, (x, y, w, h) in by.items():
        assert w >= sizes[i][0] - 0.01 and h >= sizes[i][1] - 0.01, (i, w, h)
        assert x >= ax0 - 0.01 and x + w <= ax1 + 0.01 and y >= ay0 - 0.01 and y + h <= ay1 + 0.01, (i, x, y, w, h)
        assert not (x + w > tb[0] + 0.01 and y + h > tb[1] + 0.01), i
    if col["U2"] != col["J2"]:
        gap = by["J2"][0] - (by["U2"][0] + by["U2"][2])
        assert gap >= PAGE.GUT + 3 * PAGE.TRACK - 0.01, gap              # three nets cross this channel
    # too big for A4 -> the next paper, not an overlap
    big = dict(sizes, U2=(200.0, 180.0))
    blocks, sz, F, D = _fake_page(big, rank, pin, edges=[("U1", "U2")])
    got = PAGE.compose(blocks, sz, F, D)[0]
    assert got in (("B4", "A3") if PAGE.B_SERIES else ("A3",)), got
    assert PAGE.compose(blocks, sz, F, D, paper="A4")[0] is None
    assert PAGE.dims("B4") == (353.0, 250.0) and PAGE.dims("A4") == (297, 210) and PAGE.dims("User 300x200") == (300.0, 200.0)
    # a connector drawn at a part whose block ends up in a middle column is reported for undocking
    wide = {"J1": (60.96, 152.4), "U1": (60.96, 152.4), "U2": (60.96, 152.4)}
    blocks, sz, F, D = _fake_page(wide, {"J1": 0, "U1": 1, "U2": 2}, {"J1": "first", "U1": None, "U2": None},
                                  edges=[("J1", "U1"), ("U1", "U2")], edge_blocks=["J1", "U1"], docked={"J9": "U1"})
    F.pin["U2"] = "last"
    pp, pos, info = PAGE.compose(blocks, sz, F, D)
    assert info["col_of"]["U1"] == 1 and info["undock"] == ["J9"], info
    assert PAGE.notes_rect("A4", [[20.32, 20.32, 100.0, 100.0]]) is not None


def test_cross_block_wiring_by_cost():
    """<= 60 mm, <= 4 bends, <= 2 crossings, cost = mm + 8 x bends + 20 x crossings <= 150; a wide bundle is wired
    completely or not at all; only blocks of neighbouring columns are wired."""
    U_ = route.U
    assert route._cross_ok([((0, 0), (40, 0))], [])                      # 50.8 mm straight
    assert not route._cross_ok([((0, 0), (50, 0))], [])                  # 63.5 mm: too long
    assert not route._cross_ok([((0, 0), (5, 0)), ((5, 0), (5, 5)), ((5, 5), (10, 5)), ((10, 5), (10, 9)),
                                ((10, 9), (14, 9)), ((14, 9), (14, 12))], [])     # five bends
    assert not route._cross_ok([((0, 0), (30, 0))], [(5, 0), (9, 0), (13, 0)])   # three crossings
    ln, bends, nx, cost = route.cross_cost([((0, 0), (20, 0)), ((20, 0), (20, 10))], [(7, 0)])
    assert bends == 1 and nx == 1 and abs(cost - (30 * U_ + 8 + 20)) < 1e-6

    def setup(dx_cells, n=2, wall=None):
        wires, terms, blk = [], [], {}
        for i in range(n):
            y = round(i * 2 * U_, 4)
            a0, a1 = (0.0, y), (round(4 * U_, 4), y)
            b0, b1 = (round((4 + dx_cells) * U_, 4), y), (round((8 + dx_cells) * U_, 4), y)
            wires += [(f"N{i}", a0, a1), (f"N{i}", b0, b1)]
            terms += [(f"N{i}", a1, (1, 0)), (f"N{i}", b0, (-1, 0))]
            blk[(f"N{i}", a1)] = 0
            blk[(f"N{i}", b0)] = 1
        R = route.Router([wall] if wall else [], [], {}, wires, (-20, -40, 120, 60))
        R.v2 = True
        return R, wires, terms, blk
    R, wires, terms, blk = setup(30)
    n0 = len(wires)
    done = route.route_cross(R, wires, terms, ["N0", "N1"], blk, lambda a, b: True, lambda k: 2)
    assert done == ["N0", "N1"] and len(wires) == n0 + 2, (done, len(wires))
    assert all(a[1] == b[1] for _n, a, b in wires[n0:]), "parallel straight tracks"
    assert len(route.components(wires, "N0", terms)) == 1
    R, wires, terms, blk = setup(30)
    assert route.route_cross(R, wires, terms, ["N0", "N1"], blk, lambda a, b: False, lambda k: 2) == []   # not neighbours
    R, wires, terms, blk = setup(30)
    assert route.route_cross(R, wires, terms, ["N0", "N1"], blk, lambda a, b: True, lambda k: 5) == []    # too many pins
    R, wires, terms, blk = setup(60)
    assert route.route_cross(R, wires, terms, ["N0", "N1"], blk, lambda a, b: True, lambda k: 2) == []    # 76 mm apart
    # a bus of eight, one member of which cannot be wired: nothing of it is wired and the router is as it was
    R, wires, terms, blk = setup(30, n=8)
    y7 = 7 * 2 * U_
    R._fill([10 * U_, y7 - 60.0, 12 * U_, y7 + 60.0], R.blocked)        # a wall: no member can cross here ...
    for i in range(7):                                                   # ... except through seven doors
        for c in list(R.blocked):
            if c[1] == i * 2:
                R.blocked.discard(c)
    h0, n0 = dict(R.h), len(wires)
    assert route.route_cross(R, wires, terms, [f"N{i}" for i in range(8)], blk, lambda a, b: True, lambda k: 2) == []
    assert len(wires) == n0 and R.h == h0


def test_flags_stand_on_their_net_not_on_strips():
    """A PWR_FLAG sits on a wire that reaches a component pin of its net (no flag island), upright where it can."""
    for name in BOARDS:
        pr, r, _D0, _sig = _laid(name)
        sch = O.load_sheet(pr, None, create=False)
        flags = [s for s in sch.symbols() if s.is_power and O.is_flag(s)]
        assert r["metrics"]["flag_islands"] == 0, name
        grp = {}
        for gi, g in enumerate(O.geo_groups(sch)):
            for f in g["flags"]:
                grp[id(f)] = g
        for s in flags:
            g = grp.get(id(s))
            if g is not None:
                assert g["pins"], (name, s.ref, "flag on a wire without a component pin")


def test_rail_flag_stands_on_the_rails_own_wire():
    """Work package 3: the PWR_FLAG of a rail stands upright ON a wire of that rail when one has a free spot (the
    wire from the receptacle's GND pin to its ground symbol) - not on a side wire of its own beside the symbol.
    With build.FLAG_ON_RAIL_WIRE off the earlier drawing comes back; either way the flags stay on their nets."""
    def on_wire(sch):
        """PWR_FLAGs whose pin has its net's horizontal wire running on to BOTH sides (the emitter cuts a wire at
        every point something stands on, so the flag is at two wire ends) - not at the end of a side wire."""
        hz = [(min(w["a"][0], w["b"][0]), max(w["a"][0], w["b"][0]), w["a"][1]) for w in sch.wires()
              if abs(w["a"][1] - w["b"][1]) < 1e-6]
        n = 0
        for s in sch.symbols():
            if s.is_power and O.is_flag(s):
                x, y = s.pins_world()[0]["xy"]
                lf = any(abs(y - wy) < 0.01 and x0 < x - 0.01 and abs(x1 - x) < 0.01 or
                         abs(y - wy) < 0.01 and x0 + 0.01 < x < x1 - 0.01 for x0, x1, wy in hz)
                rt = any(abs(y - wy) < 0.01 and x1 > x + 0.01 and abs(x0 - x) < 0.01 or
                         abs(y - wy) < 0.01 and x0 + 0.01 < x < x1 - 0.01 for x0, x1, wy in hz)
                n += lf and rt
        return n
    assert B.FLAG_ON_RAIL_WIRE
    for name in ("c3-devboard", "li-ion-powerbank", "phone-breathalyzer"):
        sch, D, sig0 = _memory_layout(name, "flagwire")
        assert route.verify_partition(_reload(sch), D, sig0) == [], name
        got = on_wire(sch)
        B.FLAG_ON_RAIL_WIRE = False
        try:
            sch2, D2, sig2 = _memory_layout(name, "flagside")
        finally:
            B.FLAG_ON_RAIL_WIRE = True
        assert route.verify_partition(_reload(sch2), D2, sig2) == [], name
        # package A: a flag on its rail's wire keeps build.FLAG_APART from that wire's supply / ground symbol and
        # stands on no corner or junction; where the rail's wires are too short for that (the receptacles of
        # c3-devboard and li-ion-powerbank, whose rail wires are short L's now) the flag stands beside the symbol
        # in both drawings. The long ground wire of the breathalyzer's receptacle still carries its flag.
        assert got >= on_wire(sch2), (name, got, on_wire(sch2))
        if name == "phone-breathalyzer":
            assert got > on_wire(sch2), (name, got, on_wire(sch2))
        up = [s for s in sch.symbols() if s.is_power and O.is_flag(s)]
        assert len(up) == len([s for s in sch2.symbols() if s.is_power and O.is_flag(s)]), name


def test_connector_direction_through_passives():
    """A line input behind a pot and a coupling cap is a signal source (left edge), a speaker behind the output
    cap is driven (right edge), a jack that feeds a rail through a diode is a supply input."""
    if not (UNSEEN / "audio-amp").is_dir():
        return
    d = TMP / "audio-amp-dir"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(UNSEEN / "audio-amp", d)
    pr = Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)
    D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
    by_val = {D.comps[r]["value"]: r for r in D.units}
    assert FLOW.conn_direction(D, by_val["LINE IN"]) == "in"
    assert FLOW.conn_direction(D, by_val["SPEAKER"]) == "out"
    F = FLOW.analyse(D)
    assert F.pin[D.block_of[by_val["LINE IN"]]] == "first" and F.pin[D.block_of[by_val["SPEAKER"]]] == "last", F.pin
    d = TMP / "ne555-dir"
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(UNSEEN / "ne555-blinker", d)
    pr = Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)
    D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
    jack = next(r for r in D.units if D.comps[r]["value"] == "9V DC")
    assert FLOW.conn_direction(D, jack) == "supply"


def test_unseen_designs_are_laid_out_in_pro2():
    """The designs of bench/sch/corpus_unseen (not used while the engine was developed): pro2 without fallback, the
    gate's verdict, flow and page rules. Looks are judged on the bench images, not here."""
    names = sorted(d.name for d in UNSEEN.iterdir() if d.is_dir()) if UNSEEN.is_dir() else []
    assert len(names) >= 3, names
    wired = 0
    for name in names:
        d = TMP / f"unseen-{name}"
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(UNSEEN / name, d)
        pr = Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)
        sch0 = O.load_sheet(pr, None, create=False)
        D0 = Design(sch0, export_netlist(pr))
        r = engine.relayout(pr, force=True, style="pro2")
        assert r["style_used"] == "pro2", (name, r.get("tried"))
        assert r["connectivity"].startswith("identical"), name
        m = r["metrics"]
        assert m["flow_violations"] == 0 and m["connector_edge_ratio"] == 1.0, (name, m["flow_violations"], m["connector_edge_ratio"])
        assert m["border_min_mm"] >= 10 and m["off_centre"] <= 0.07 and m["flag_islands"] == 0, name
        assert m["overlaps"] == 0, (name, m.get("overlap_list"))
        assert not route.verify_partition(O.load_sheet(pr, None, create=False), D0), name
        wired += r["page"]["cross_block_wires"]
    assert wired >= 1, "no net was wired across two blocks on any unseen design"


# ───────────────────────── work package 4: text, symbols, notes, title block ─────────────────────────
from copper_kicad.sch.pro import emit as EMIT  # noqa: E402
from copper_kicad.sch.pro import gate as GATE  # noqa: E402
from copper_kicad.sch.pro import notes as NOTES  # noqa: E402
from copper_kicad.sch.pro import symfix as SYMFIX  # noqa: E402
from copper_kicad.sch.pro import text as TEXT  # noqa: E402

# measured at the end of work package 4: the two supply pins of the STM32 that sit between wired pins on one side
# (VDDA / VSSA) keep a sideways symbol - an upright one would cross three wires
# package A: the two of the STM32 are upright; three pins without any upright place short of an arch lie on their side
# (see SIDEWAYS_MAX)
HARD_LEFT = {"c3-devboard": ["power_sideways=1"], "li-ion-powerbank": ["power_sideways=1"], "rp2040-min": ["power_sideways=1"]}


def test_wp4_bench_numbers():
    for name in BOARDS:
        pr, r, D0, _sig = _laid(name)
        m = r["metrics"]
        assert m["hard"] == HARD_LEFT.get(name, []), (name, m["hard"])
        assert m["overlaps"] == 0 and m["wire_through_text_or_body"] == 0, (name, m.get("overlap_list"))
        assert m["labels_vertical"] == 0, name
        # (fix round 5) the rails line stands in the title block and counts as the sheet's note about its supplies:
        # the free NOTES block that repeated it beside the title block is gone (notes.RAILS_IN_TITLE_BLOCK)
        assert m["title_rails"] == 1 and m["notes_count"] + m["title_rails"] >= 2, (name, m["notes_count"])
        assert m["title_fields"] >= 3, (name, m["title_fields"])
        assert r["notes"]["placed"] >= 1, (name, r["notes"])
        assert not (r["text"] or {}).get("unplaced"), (name, r["text"])
        # the notes belong to the engine's groups: they are not "user graphics", so the sheet is still the engine's
        sch1 = O.load_sheet(pr, None, create=False)
        assert not EMIT.user_graphics(sch1), name
        assert engine.sheet_state(sch1) == "engine", name
        tb = sexpr.find(sch1.tree, "title_block")
        cm = {int(e[1]): str(e[2]) for e in sexpr.find_all(tb, "comment")}
        assert cm.get(1) and cm.get(2, "").startswith("Rails: ") and cm.get(EMIT.MACHINE_SLOT) == EMIT.MACHINE_LINE and 4 not in cm, (name, cm)
        assert len(cm[1]) <= 80 and len(cm[2]) <= 80, (name, cm)


def test_relayout_of_a_pro2_sheet_changes_nothing():
    """Notes, title block and re-seated texts are deterministic: a second run finds its own drawing."""
    pr = _board("c3-devboard", "twice")
    r1 = engine.relayout(pr, force=True, style="pro2", symfix="auto")
    assert r1["style_used"] == "pro2" and r1.get("symfix"), r1.get("tried")
    before = pr.sch.read_text(encoding="utf-8")
    r2 = engine.relayout(pr, style="pro2", symfix="auto")
    assert r2["style_used"] == "unchanged", (r2["style_used"], r2.get("reason"))
    assert pr.sch.read_text(encoding="utf-8") == before


def test_user_comments_in_the_title_block_survive():
    pr = _board("opamp-afe", "tb")
    sch = O.load_sheet(pr, None, create=False)
    tb = sexpr.find(sch.tree, "title_block")
    if tb is None:
        tb = [sexpr.Sym("title_block")]
        sch.tree.insert(5, tb)
    tb[:] = [e for e in tb if not (isinstance(e, list) and e and e[0] == "comment")]
    tb.append([sexpr.Sym("comment"), 1, "Checked by QA"])
    pr.sch.write_text(sch.text(), encoding="utf-8", newline="\n")
    r = engine.relayout(pr, force=True, style="pro2")
    assert r["style_used"] == "pro2", r.get("tried")
    tb = sexpr.find(O.load_sheet(pr, None, create=False).tree, "title_block")
    cm = {int(e[1]): str(e[2]) for e in sexpr.find_all(tb, "comment")}
    assert cm[1] == "Checked by QA" and cm.get(2, "").startswith("Rails: "), cm


def test_notes_are_derived_from_the_netlist_only():
    assert NOTES.parse_value("4.7k", "R") == 4700.0 and NOTES.parse_value("4k7", "R") == 4700.0
    assert NOTES.parse_value("100", "R") == 100.0 and NOTES.parse_value("1M", "R") == 1e6
    assert abs(NOTES.parse_value("100nF", "C") - 1e-7) < 1e-15 and abs(NOTES.parse_value("22uH", "L") - 22e-6) < 1e-12
    assert NOTES.parse_value("12MHz", "F") == 12e6
    for junk, kind in (("LED red", "R"), ("TS-1187A", "R"), ("100", "C"), ("", "R"), ("10k", "C")):
        assert NOTES.parse_value(junk, kind) is None, (junk, kind)
    assert NOTES.rail_volts("+3V3") == 3.3 and NOTES.rail_volts("+5V") == 5.0 and NOTES.rail_volts("VBUS") == 5.0
    assert NOTES.rail_volts("VBAT") is None
    pr = _board("li-ion-powerbank", "notes")
    nl = export_netlist(pr)
    D = Design(O.load_sheet(pr, None, create=False), nl)
    a = NOTES.build(D)
    b = NOTES.build(Design(O.load_sheet(pr, None, create=False), nl))
    assert a == b                                           # deterministic
    flat = [ln for lst in a.values() for ln in lst]
    assert any("Vout = Vref x (1 + R8/R9) = 0.6 V x 8.33 = 5.00 V" in ln for ln in flat), flat      # MT3608, 22k / 3k
    assert any(ln.startswith("D2: I = (5 V - Vf 2 V) / 1k = 3 mA") for ln in flat), flat
    # (fix round 5) the rail sources are written once, in the title block; the sheet-level lines exist behind the switch
    assert None not in a and NOTES.RAILS_IN_TITLE_BLOCK
    NOTES.RAILS_IN_TITLE_BLOCK = False
    try:
        old = NOTES.build(D)
    finally:
        NOTES.RAILS_IN_TITLE_BLOCK = True
    assert any(ln.startswith("+5V: from U3 (MT3608)") for ln in old[None]), old[None]
    assert all(len(ln) <= NOTES.MAX_CHARS for ln in flat)
    # a value that cannot be parsed gives no note at all - nothing is guessed
    for s in D.units["R8"]:
        s.set_field("Value", "see BOM")
    D2 = Design(D.sch, nl)
    flat2 = [ln for lst in NOTES.build(D2).values() for ln in lst]
    assert any("Vout = Vref x (1 + R8/R9)" in ln and "5.00" not in ln for ln in flat2), flat2
    pr = _board("stm32-sensor", "notes")
    D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
    flat = [ln for lst in NOTES.build(D).values() for ln in lst]
    assert any(ln.startswith("Y1: 8MHz, load caps C1/C2 30pF (CL = 15 pF + stray)") for ln in flat), flat
    assert any(ln.startswith("I2C pull-ups R1, R2 = 4.7k to +3V3") for ln in flat), flat
    assert any("JST PH 2-pin" in ln for ln in flat), flat


def test_text_solver_reseats_only_what_touches_something():
    sch, D, _sig = _memory_layout("motor-driver", "text")
    sch = _reload(sch)
    assert TEXT.solve(sch)["moved"] == 0                    # the engine's own drawing is already clean
    nl = D.nl
    m0 = score.measure(sch, nl)
    assert m0["overlaps"] == 0
    # push the value of one resistor onto the body of the nearest other part, and one reference onto a wire
    parts = [s for s in sch.symbols(include_power=False)]
    r = next(s for s in parts if s.ref.startswith("R"))
    other = min((s for s in parts if s.ref != r.ref), key=lambda s: abs(s.at[0] - r.at[0]) + abs(s.at[1] - r.at[1]))
    gb = TEXT.graphic_box(sch, other)
    a = sexpr.find(r.field_node("Value"), "at")
    a[1], a[2] = (gb[0] + gb[2]) / 2, (gb[1] + gb[3]) / 2
    w = max(sch.wires(), key=lambda w: abs(w["a"][0] - w["b"][0]))
    c = next(s for s in parts if s.ref.startswith("C"))
    a2 = sexpr.find(c.field_node("Reference"), "at")
    a2[1], a2[2] = (w["a"][0] + w["b"][0]) / 2, w["a"][1]
    bad = score.measure(sch, nl)
    assert bad["overlaps"] >= 1 and bad["wire_through_text_or_body"] >= 1, (bad["overlaps"], bad["wire_through_text_or_body"])
    res = TEXT.solve(sch)
    assert res["conflicts"] >= 2 and res["moved"] >= 2 and not res["unplaced"], res
    m1 = score.measure(sch, nl)
    assert m1["overlaps"] == 0 and m1["wire_through_text_or_body"] == 0, (m1.get("overlap_list"), m1["wire_through_text_or_body"])
    # the re-seated texts stay next to their part
    for s, fld in ((r, "Value"), (c, "Reference")):
        at = sexpr.find(s.field_node(fld), "at")
        assert abs(float(at[1]) - s.at[0]) + abs(float(at[2]) - s.at[1]) < 16, (s.ref, at)
    # nothing electrical moved
    assert not route.verify_partition(sch, D)


def test_text_width_matches_kicad_rendering():
    """model.text_width against a kicad-cli rendering of 28 strings (bench/sch/textcheck.py)."""
    sys.path.insert(0, str(ROOT / "bench" / "sch"))
    import textcheck
    rows = textcheck.measure(TMP / "textcheck")
    assert len(rows) >= 25, len(rows)
    short = [r for r in rows if " " not in r["text"]]
    assert max(r["error_mm"] for r in short) <= 0.6, sorted(short, key=lambda r: -r["error_mm"])[:3]
    assert min(r["error_mm"] for r in short) >= -0.35, sorted(short, key=lambda r: r["error_mm"])[:3]
    assert max(r["error_mm"] / r["kicad_mm"] for r in rows) <= 0.045, rows


_STACKED = """(symbol "T:STK" (pin_names (offset 1.016)) (exclude_from_sim no) (in_bom yes) (on_board yes)
 (property "Reference" "J" (at 0 8 0) (effects (font (size 1.27 1.27))))
 (property "Value" "STK" (at 0 -8 0) (effects (font (size 1.27 1.27))))
 (symbol "STK_0_1" (rectangle (start -5.08 5.08) (end 5.08 -2.54) (stroke (width 0.254) (type default)) (fill (type background))))
 (symbol "STK_1_1"
  (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VBUS" (effects (font (size 1.27 1.27)))) (number "A4" (effects (font (size 1.27 1.27)))))
  (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VBUS" (effects (font (size 1.27 1.27)))) (number "B9" (effects (font (size 1.27 1.27)))))
  (pin passive line (at -7.62 0 0) (length 2.54) (name "CC" (effects (font (size 1.27 1.27)))) (number "A5" (effects (font (size 1.27 1.27)))))
  (pin passive line (at 7.62 2.54 180) (length 2.54) (name "D+" (effects (font (size 1.27 1.27)))) (number "A6" (effects (font (size 1.27 1.27)))))
  (pin passive line (at 7.62 2.54 180) (length 2.54) (name "D+" (effects (font (size 1.27 1.27)))) (number "B6" (effects (font (size 1.27 1.27)))))))"""


def _pin_at(tree, number):
    for sub in sexpr.find_all(tree, "symbol"):
        for p in sexpr.find_all(sub, "pin"):
            if str(sexpr.find(p, "number")[1]) == number:
                a = sexpr.find(p, "at")
                return float(a[1]), float(a[2])
    raise KeyError(number)


def test_symfix_level1_detects_and_level2_derives():
    ls = sexpr.loads(_STACKED)
    d = SYMFIX.defects(ls)
    assert d["stacks"] == [["A4", "B9"], ["A6", "B6"]] and not d["long"], d
    tree, info = SYMFIX.derive(ls)
    assert info["fanned"] == 2 and not SYMFIX.defects(tree)["stacks"], info
    # the FIRST pin of every stack keeps its tip; the second moved to a free neighbouring row (not onto A5's)
    assert _pin_at(tree, "A4") == (-7.62, 2.54) and _pin_at(tree, "A6") == (7.62, 2.54)
    assert _pin_at(tree, "B9") == (-7.62, 5.08), _pin_at(tree, "B9")            # down is taken by A5: up
    assert _pin_at(tree, "B6") == (7.62, 0.0), _pin_at(tree, "B6")
    rect = sexpr.find(sexpr.find_all(tree, "symbol")[0], "rectangle")
    ys = sorted((float(sexpr.find(rect, "start")[2]), float(sexpr.find(rect, "end")[2])))
    assert ys[0] <= -1.27 and ys[1] >= 6.35, ys                                  # the body grew to hold B9
    assert SYMFIX.equivalent(ls, tree) == []
    assert SYMFIX.derive(tree) is None                                           # never derived twice
    # the corpus USB-C: one pin per pad pair with a five-character number on a 2.54 mm pin
    pr = _board("c3-devboard", "symfix")
    sch = O.load_sheet(pr, None, create=False)
    sc = SYMFIX.scan(sch)
    assert len(sc) == 1 and next(iter(sc.values()))["long"] == ["A1B12", "A4B9", "B1A12", "B4A9"], sc
    assert any("J1" in h and "overprint" in h for h in SYMFIX.hints(sch))
    key = next(iter(sc))
    orig = sch.lib_symbol(key)
    tips0 = sorted((p["number"], tuple(p["xy"])) for s in sch.symbols() if s.ref == "J1" for p in s.pins_world())
    got = SYMFIX.apply(sch)
    assert list(got) == [key] and got[key]["offset"] and got[key]["refs"] == ["J1"], got
    assert not SYMFIX.scan(sch) and SYMFIX.equivalent(orig, sch.lib_symbol(key)) == []
    tips1 = sorted((p["number"], tuple(p["xy"])) for s in sch.symbols() if s.ref == "J1" for p in s.pins_world())
    assert tips0 == tips1                                                        # no pin tip moved
    assert SYMFIX.number_strips(sch)                                             # the strips are known obstacles


def test_symfix_engine_level2_or_hint():
    pr, r, _D0, _sig = _laid("c3-devboard")                                      # default: symfix="hint" (Level 1)
    assert r["style_used"] == "pro2" and "symfix" not in r
    assert "J1" in r.get("hint", "") and "overprint" in r["hint"], r.get("hint")
    assert SYMFIX.scan(O.load_sheet(pr, None, create=False))
    pr2 = _board("c3-devboard", "symfix-auto")
    nl0 = export_netlist(pr2)
    r2 = engine.relayout(pr2, force=True, style="pro2", symfix="auto")
    assert r2["style_used"] == "pro2" and not r2.get("tried"), r2.get("tried")
    assert r2["connectivity"].startswith("identical") and r2["symfix"], r2.get("symfix")
    assert "overprint" not in r2.get("hint", "")
    sch2 = O.load_sheet(pr2, None, create=False)
    assert not SYMFIX.scan(sch2)
    nl1 = export_netlist(pr2)
    assert {k: sorted(map(str, v)) for k, v in nl0["nets"].items()} == {k: sorted(map(str, v)) for k, v in nl1["nets"].items()}
    j1 = next(s for s in sch2.symbols() if s.ref == "J1")
    assert sexpr.find(j1.node, "lib_name") is None                               # same key: nothing for the netlist to see
    pr3 = _board("c3-devboard", "symfix-off")
    r3 = engine.relayout(pr3, force=True, style="pro2", symfix="off")
    assert "overprint" not in r3.get("hint", "")


def test_gate_accepts_symfix_only_on_pin_equivalence():
    """The one gate relaxation. A derived embedded symbol passes G0 only with symfix=True AND when every pin and
    property equals the original's; a changed pin type, a renamed pin, a hidden pin, an edited property, a
    lib_name redirect are each refused. Without symfix=True nothing about the gate changed."""
    pr = _board("c3-devboard", "gate-symfix")
    spath = O.load_sheet(pr, None, create=False).sheet_path
    b = GATE.baseline(pr, pr.sch, spath, with_erc=False)
    try:
        def cand(mutate=None, lib_name=False):
            sch = O.load_sheet(pr, None, create=False)
            got = SYMFIX.apply(sch, lib_name=lib_name)
            key = next(iter(got))
            if mutate:
                mutate(sch.lib_symbol(key))
                sch._pin_cache.clear()
            sch.prune_lib_symbols()
            return sch

        def pin(ls, number):
            for sub in sexpr.find_all(ls, "symbol"):
                for p in sexpr.find_all(sub, "pin"):
                    if str(sexpr.find(p, "number")[1]) == number:
                        return p
            raise KeyError(number)
        good = cand()
        ok, why = GATE.symfix_ok(b.libsyms, good)
        assert len(ok) == 1 and not why, (ok, why)
        assert GATE.payload_diff(b.payload, GATE.symbol_payload(good), ok) == []
        refused = GATE.payload_diff(b.payload, GATE.symbol_payload(good))            # the unchanged gate
        assert len(refused) == 1 and "embedded library symbol would change" in refused[0], refused
        g = GATE.check(pr, b, good.text(), spath)
        assert not g.ok and any(x.startswith("G0 symbol J1") for x in g.reasons), g.reasons
        g = GATE.check(pr, b, good.text(), spath, symfix=True)
        assert g.ok and g.hash_before == g.hash_after, g.reasons

        def retype(ls):
            pin(ls, "A5")[1] = sexpr.Sym("power_out")

        def rename(ls):
            sexpr.find(pin(ls, "A5"), "name")[1] = "CC1_X"

        def hide(ls):
            pin(ls, "A8").append([sexpr.Sym("hide"), sexpr.Sym("yes")])

        def renumber(ls):
            sexpr.find(pin(ls, "B8"), "number")[1] = "B9"

        def prop(ls):
            next(p for p in sexpr.find_all(ls, "property") if str(p[1]) == "Footprint")[2] = "Other:Footprint"

        def flag(ls):
            sexpr.find(ls, "in_bom")[1] = sexpr.Sym("no")

        def drop(ls):
            for sub in sexpr.find_all(ls, "symbol"):
                for p in sexpr.find_all(sub, "pin"):
                    if str(sexpr.find(p, "number")[1]) == "A8":
                        sub.remove(p)
        for mut in (retype, rename, hide, renumber, prop, flag, drop):
            bad = cand(mut)
            ok, why = GATE.symfix_ok(b.libsyms, bad)
            assert not ok and why and "not pin-equivalent" in why[0], (mut.__name__, ok, why)
            g = GATE.check(pr, b, bad.text(), spath, symfix=True)
            assert not g.ok and any("symfix" in x or x.startswith("G0") for x in g.reasons), (mut.__name__, g.reasons)
        # the variant of the design document - a separate "<name>_cu" symbol selected with lib_name - is refused:
        # by G0 (the part would point to another symbol), and KiCad would write the lib_name into the netlist
        named = cand(lib_name=True)
        g = GATE.check(pr, b, named.text(), spath, symfix=True)
        assert not g.ok and any("another embedded library symbol" in x for x in g.reasons), g.reasons
    finally:
        b.close()
    # ERC: the library-symbol checks stay exempt in general, but not for the parts symfix touched
    import json as _json
    k_j1 = _json.dumps(["warning", "lib_symbol_mismatch", ["Symbol J1 [TYPE-C-31-M-12_C165948]"]])
    k_pwr = _json.dumps(["warning", "lib_symbol_issues", ["Symbol #PWR [GND]"]])
    k_r1 = _json.dumps(["warning", "lib_symbol_mismatch", ["Symbol R1 [R]"]])
    after = {k_j1: 1, k_pwr: 9, k_r1: 1}
    assert GATE.erc_new({k_pwr: 2}, after) == []
    new = GATE.erc_new({k_pwr: 2}, after, strict_refs={"J1"})
    assert len(new) == 1 and "lib_symbol_mismatch" in new[0] and "J1" in new[0], new
    assert GATE.erc_new({k_pwr: 2, k_j1: 1}, after, strict_refs={"J1"}) == []


def test_upright_power_by_crossing_and_upright_names():
    """li-ion-powerbank: the charger's ground pin sits between two wired pins - its symbol hangs upright from an L
    that crosses ONE wire squarely, clear of junctions; the name of the D+/D- short on the USB socket's bottom
    pins reads horizontally."""
    pr, r, _D0, _sig = _laid("li-ion-powerbank")
    m = r["metrics"]
    # (package A: the exposed pad EP - ground above four wired rows - is the one symbol that lies on its side; the
    # crossings are those of PA_CROSSINGS)
    assert m["power_sideways"] <= 1 and m["labels_vertical"] == 0 and m["crossing_near_junction"] == 0, m["hard"]
    assert m["power_hooks"] == 0 and m["power_wrong_side"] == 0, m["hard"]
    assert m["wire_crossings"] <= PA_CROSSINGS["li-ion-powerbank"], m["wire_crossings"]
    sch = O.load_sheet(pr, None, create=False)
    for l in sch.labels():
        assert int(round(float(l.get("rot", 0) or 0))) % 180 == 0, l["net"]


def test_sch_tidy_tool_offers_pro2_and_symfix():
    from copper_kicad.sch.pro import tool as TOOL
    pr = _board("opamp-afe", "tool")
    out = TOOL.tidy(pr, force=True, style="pro2", symfix="auto")
    assert out["style_used"] == "pro2" and out["notes"] >= 1 and "hard" not in out, out      # (fix round 5: rails in the title block)
    # (package B: 91 -> 84; the old score counts wired pin pairs, and VREF is two names now instead of a three-bend wire)
    assert out["readability"]["after"] >= 80, out["readability"]
    import json as _json
    assert len(_json.dumps(out)) < 1400, len(_json.dumps(out))
    try:
        engine.relayout(pr, style="pro2", symfix="always")
        raise AssertionError("symfix='always' must be refused")
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "code", "") == "BAD_ARGUMENT", e


def test_symfix_never_redraws_a_stock_symbol():
    """Owner rule "default symbols first": Level 2 is confined to Copper-generated symbols (copper-lcsc). The same
    defective symbol under a stock library nickname is left as it is - by symfix.apply, by the engine (symfix='auto'
    lays the sheet out with the Level 1 hint) and by the gate clause (an equivalent redraw of it is refused)."""
    assert SYMFIX.may_redraw("copper-lcsc:TYPE-C-31-M-12_C165948")
    for k in ("Connector:USB_C_Receptacle_USB2.0_16P", "Device:R", "mylib:USB_C", "USB_C"):
        assert not SYMFIX.may_redraw(k), k
    pr = _board("c3-devboard", "symfix-stock")
    key = next(iter(SYMFIX.scan(O.load_sheet(pr, None, create=False))))
    stock = "Connector:" + key.split(":", 1)[1]
    for f in list(pr.dir.glob("*.kicad_sch")):
        f.write_text(f.read_text(encoding="utf-8").replace(key, stock), encoding="utf-8")
    sch = O.load_sheet(pr, None, create=False)
    assert stock in SYMFIX.scan(sch)                                             # the defect is still detected ...
    before = sexpr.dumps(sch.lib_symbol(stock))
    assert SYMFIX.apply(sch) == {} and sexpr.dumps(sch.lib_symbol(stock)) == before   # ... and nothing is redrawn
    assert any("left exactly as its library draws it" in h for h in SYMFIX.hints(sch)), SYMFIX.hints(sch)
    # the gate clause: even a pin-equivalent redraw of a non-generated symbol is refused
    spath = sch.sheet_path
    b = GATE.baseline(pr, pr.sch, spath, with_erc=False)
    try:
        forged = O.load_sheet(pr, None, create=False)
        tree, _info = SYMFIX.derive(forged.lib_symbol(stock))
        forged.embed_tree(stock, tree)
        ok, why = GATE.symfix_ok(b.libsyms, forged)
        assert not ok and why and "not a Copper-generated symbol" in why[0], (ok, why)
    finally:
        b.close()
    r = engine.relayout(pr, force=True, style="pro2", symfix="auto")
    assert r["style_used"] == "pro2" and "symfix" not in r, (r.get("style_used"), r.get("symfix"))
    assert "left exactly as its library draws it" in str(r.get("hint")), r.get("hint")
    assert sexpr.dumps(O.load_sheet(pr, None, create=False).lib_symbol(stock)) == before


def test_names_keep_clear_of_junctions_and_words_keep_apart():
    """Two reading rules of work package 4. (1) A net name that stands ON its wire does not stand over a junction
    of that wire - also not over the junction a part pin makes that ends on the wire (the filter cap under
    "AIN_F"). (2) A Reference / Value never stands closer than text.WORD_AIR to the value text of a power symbol
    or PWR_FLAG on the same line ("PWR_FLAG330n"); (3) nor do two such power texts stand that close to each other
    (build.Blk.words_apart)."""
    from copper_kicad.sch import layout as LAY
    seen = 0
    for name in BOARDS:
        pr = _laid(name)[0]
        sch = O.load_sheet(pr, None, create=False)
        tips = [tuple(pn["xy"]) for s in sch.symbols(include_power=False) for pn in s.pins_world()]
        hz = [(min(w["a"][0], w["b"][0]), max(w["a"][0], w["b"][0]), w["a"][1]) for w in sch.wires()
              if abs(w["a"][1] - w["b"][1]) < 1e-6]
        for l in sch.labels():
            if int(round(float(l.get("rot", 0) or 0))) % 360 != 0:
                continue
            x, y = l["at"]
            # only names that stand on a wire running on to both sides (a name at a wire end is not "on" it)
            if not (any(x0 - 0.01 <= x <= x1 + 0.01 and abs(yy - y) < 0.01 and x0 < x - 0.01 for x0, x1, yy in hz)
                    and any(x0 - 0.01 <= x <= x1 + 0.01 and abs(yy - y) < 0.01 and x1 > x + 0.01 for x0, x1, yy in hz)):
                continue
            seen += 1
            b = LAY.label_box(l)
            under = [q for q in tips if abs(q[1] - y) < 0.01 and b[0] + 0.3 < q[0] < b[2] - 0.3]
            assert not under, (name, l["net"], under)
        sc = TEXT.scene(sch)
        for ref, _i, fb in sc["field"]:
            for pb in sc["ptext"]:
                assert not TEXT._words_touch(fb, pb), (name, ref, fb, pb)
        # (3) two power value texts on one line ("GND VBUS", "+5V PWR_FLAG") keep at least 0.7 mm of air
        pt = sc["ptext"]
        for i in range(len(pt)):
            for j in range(i + 1, len(pt)):
                assert not TEXT._words_touch(pt[i], pt[j], 0.7), (name, pt[i], pt[j])
    assert seen >= 5, seen
    # the rule itself, on a bare wire: a pin tip in the middle of the run pushes the name off it
    wires = [("N", (0.0, 10.16), (25.4, 10.16))]
    free = engine._name_on_wire("N", ("AIN_F", "local"), wires, [], [])
    assert free is not None
    lb = B.lbl_box("AIN_F", free, (1, 0), "local")
    mid = (round((lb[0] + lb[2]) / 2 / 1.27) * 1.27, 10.16)
    moved = engine._name_on_wire("N", ("AIN_F", "local"), wires, [], [], [mid])
    assert moved is not None and moved != free, (free, moved)
    lb2 = B.lbl_box("AIN_F", moved, (1, 0), "local")
    assert not (lb2[0] < mid[0] < lb2[2]), (lb2, mid)


# ───────────────────────── fix round 2 (three critics, 6.9 / 10) ─────────────────────────
def _laid_unseen(name: str):
    key = "unseen:" + name
    if key not in _CACHE:
        d = TMP / f"{name}-fix2"
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(UNSEEN / name, d)
        pr = Project(d, sorted(d.glob("*.kicad_pro"))[0].stem)
        sig0 = route.signature(O.load_sheet(pr, None, create=False))
        r = engine.relayout(pr, force=True, style="pro2")
        _CACHE[key] = (pr, r, sig0)
    return _CACHE[key]


def test_fix2_switch_contacts_share_one_ground_symbol():
    """The doubled contacts of a 4-pin tactile switch (pins 1 / 2 and 3 / 4, left and right of one row). History: fix
    round 2 drew a bar under the part, round 3 two ground symbols and two names, round 4 a bar over the part - each
    read as a wire rectangle or a duplicated label.
    Package B: the switch is the two-terminal part it is. Either contact pair is joined by the SHORTEST STRAIGHT LINK
    (one wire from pin tip to pin tip along the pin row, on the line the symbol itself draws between the two pins),
    the ground pair has ONE ground symbol straight under one tip, and no wire of the switch's nets runs over or under
    the part. Where the button hangs on the wire of the pin it serves (node template, `idioms.passive._hang_twin`)
    the net is one piece from that pin to the switch. Still true: no Reference / Value text against a supply symbol."""
    assert B.CONTACT_LINK and IM.TWIN_CONTACTS and not B.RAIL_PAIR_BAR
    from copper_kicad.sch import layout as LAY
    from copper_kicad.sch.pro import text as TEXT
    seen = hung = 0
    for name in ("rp2040-min", "stm32-sensor"):
        pr, r, _D0, _sig = _laid(name)
        sch, D, grp, xy, _ms, groups = _after(pr)
        gnd = [tuple(round(v, 2) for v in s.pins_world()[0]["xy"]) for s in sch.symbols()
               if s.is_power and LAY.is_ground(s.value)]
        wires = [(tuple(round(v, 2) for v in w["a"]), tuple(round(v, 2) for v in w["b"])) for w in sch.wires()]
        for ref in sorted(D.units):
            if D.npins(ref) != 4 or D.is_conn(ref) or len(set(D.pins_of[ref].values())) != 2:
                continue
            pins = {p["number"]: p for p in D.units[ref][0].pins_world()}
            rows = {}
            for no, k in D.pins_of[ref].items():
                rows.setdefault(k, []).append(pins[no])
            ys = sorted(round(p["xy"][1], 2) for p in pins.values())
            xs = sorted(round(p["xy"][0], 2) for p in pins.values())
            for k, ps in rows.items():
                assert len(ps) == 2 and {tuple(p["out"]) for p in ps} == {(-1, 0), (1, 0)}, (name, ref)
                seen += 1
                a, b = sorted(tuple(round(v, 2) for v in p["xy"]) for p in ps)
                # joined along the row ...
                assert grp[(ref, ps[0]["number"])] == grp[(ref, ps[1]["number"])], (name, ref, "contact pair not joined")
                cover = sorted((min(p[0], q[0]), max(p[0], q[0])) for p, q in wires
                               if p[1] == q[1] == a[1] and max(p[0], q[0]) > a[0] and min(p[0], q[0]) < b[0])
                assert cover and cover[0][0] <= a[0] + 0.01 and max(c[1] for c in cover) >= b[0] - 0.01, (name, ref, cover)
                if D.is_gnd(k):
                    near = [q for q in gnd if xs[0] - 7.7 <= q[0] <= xs[-1] + 7.7 and 0 < q[1] - a[1] <= 7.7]
                    assert len(near) == 1, (name, ref, near)               # ONE ground symbol, under a tip
                    assert any(abs(near[0][0] - t[0]) < 0.01 for t in (a, b)), (name, ref, near)
                elif any(grp[(ref, ps[0]["number"])] == grp[p] for p in D.view.nets[k].pins if p[0] != ref):
                    hung += 1
            # ... and nothing of it over the part or under it: no horizontal wire that spans the part within three
            # rows above its upper or below its lower contact row (the bar of round 2 / round 4)
            for p, q in wires:
                if p[1] == q[1] and min(p[0], q[0]) <= xs[0] + 0.01 and max(p[0], q[0]) >= xs[-1] - 0.01:
                    assert not (0.5 < ys[0] - p[1] <= 7.7 or 0.5 < p[1] - ys[-1] <= 7.7), (name, ref, "bar", p, q)
        assert r["metrics"]["overlaps"] == 0, (name, r["metrics"].get("overlap_list"))
        sc = TEXT.scene(sch)
        for s in sch.symbols(include_power=False):
            for p in TEXT.visible_fields(s):
                fb = TEXT.field_box(p, s)
                assert not any(TEXT._words_touch(fb, b, TEXT.SYM_AIR) for b in sc["pbody"]), (name, s.ref, str(p[2]))
    assert seen >= 6 and hung >= 3, (seen, hung)      # RUN and BOOTSEL (rp2040-min), NRST (stm32-sensor): on the wire


def test_fix2_no_wire_wraps_between_two_multi_pin_parts():
    """A named net between two multi-pin parts is wired only when the wire goes there directly (at most route.WRAP
    cells longer than the straight way). rs485-node: MISO used to run over two supply symbols to the ISP header and
    RS485_B around the termination resistor to the terminal block while RS485_A was a name - now all the lines of one
    connector are drawn the same way."""
    assert route.WRAP <= 4
    pr, r, sig0 = _laid_unseen("rs485-node")
    assert r["style_used"] == "pro2", r.get("tried")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0
    for conn, nets in (("J3", ("MISO", "MOSI", "SCK")), ("J2", ("RS485_A", "RS485_B"))):
        ways = set()
        for nm in nets:
            k = next(k for k in D.view.nets if D.text(k) == nm)
            cp = next(p for p in D.view.nets[k].pins if p[0] == conn)
            other = [p for p in D.view.nets[k].pins if p[0] != conn and D.npins(p[0]) >= 8]
            ways.add(any(grp[cp] == grp[p] for p in other))
        assert len(ways) == 1, (conn, ways)
    assert r["metrics"]["wire_crossings"] == 0


def test_fix2_neighbour_pin_ties_to_the_stem_and_jack_feeds_through_its_diode():
    """ne555-blinker: TRIG is linked to THRES by one short wire at the pins (it ran around the timing capacitor);
    the DC jack that feeds +9V through a Schottky is a supply input: jack, diode and rail in ONE frame, the jack's
    pins looking into the sheet, the diode right of it."""
    pr, r, sig0 = _laid_unseen("ne555-blinker")
    assert r["style_used"] == "pro2", r.get("tried")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0
    u = next(a for a in D.units if "555" in D.units[a][0].value)
    names = {no: D.pin_name.get((u, no), "") for no in D.pins_of[u]}
    trig = next(no for no, nm in names.items() if nm.upper().startswith("TR"))
    thr = next(no for no, nm in names.items() if nm.upper().startswith("THR"))
    assert D.pins_of[u][trig] == D.pins_of[u][thr] and grp[(u, trig)] == grp[(u, thr)]
    t = xy[(u, trig)]
    out = next(p["out"] for p in D.units[u][0].pins_world() if p["number"] == trig)
    end = (round(t[0] + out[0] * 2.54, 2), round(t[1], 2))
    ws = [w for w in sch.wires() if any(abs(e[0] - end[0]) < 0.01 and abs(e[1] - end[1]) < 0.01 for e in (w["a"], w["b"]))]
    assert len(ws) == 2, ws                                   # the stub and the link, nothing else
    assert sum(abs(w["a"][0] - w["b"][0]) + abs(w["a"][1] - w["b"][1]) for w in ws) <= 5.09
    jack = next(a for a in sorted(D.units) if D.is_conn(a) and any(D.text(k) == "VIN_RAW" for k in D.pins_of[a].values()))
    info = next(i for i in D.blocks if jack in i.anchors)
    assert info.kind == "input", info.kind
    d1 = [s for s in info.sats if s.startswith("D")]
    assert d1, info.sats
    jp = [p for p in D.units[jack][0].pins_world() if D.conn(D.pins_of[jack].get(p["number"]))]
    assert all(tuple(p["out"]) == (1, 0) for p in jp), [(p["number"], p["out"]) for p in jp]
    assert min(xy[(d1[0], no)][0] for no in D.pins_of[d1[0]]) > max(p["xy"][0] for p in jp)
    assert r["metrics"]["overlaps"] == 0 and r["metrics"]["wire_crossings"] == 0


def test_fix2_pull_ups_without_room_at_their_pins_stand_in_one_row():
    """rs485-node: the I2C pull-ups find no room on the dense pin column of the ATmega (the crystal hangs over the
    SDA / SCL rows). Fix round 2 stood them side by side as an island on one row; since class L (engine._rehome)
    they are drawn at the OTHER pin of their nets, the sensor's, each on the wire of its pin, next to each other.
    With the re-homing switched off they stand in one row again (dock.build_block, `pull_rail`)."""
    pr, r, _sig0 = _laid_unseen("rs485-node")
    sch, D, grp, xy, _ms, _groups = _after(pr)

    def pulls(D):
        rs = []
        for ref in sorted(D.units):
            ns = list(D.pins_of[ref].values())
            if ref.startswith("R") and len(ns) == 2 and any(D.text(k) in ("SDA", "SCL") for k in ns) and any(
                    D.is_rail(k) and not D.is_gnd(k) for k in ns):
                rs.append(ref)
        return rs
    rs = pulls(D)
    assert len(rs) == 2, rs
    xs = sorted(D.units[ref][0].at[0] for ref in rs)
    assert xs[1] - xs[0] <= 25.4, xs
    for ref in rs:
        (pn, k), = [(pn, k) for pn, k in D.pins_of[ref].items() if not D.is_rail(k)]
        ics = [q for q in D.view.nets[k].pins if D.npins(q[0]) >= 3]
        assert any(grp[(ref, pn)] == grp[q] for q in ics), (ref, "the pull-up is not wired to a pin of its net")
    engine.REHOME = False
    try:
        sch, D, _sig = _memory_layout_unseen("rs485-node", "norehome")
    finally:
        engine.REHOME = True
    rs = pulls(D)
    ys = {round(D.units[ref][0].at[1], 2) for ref in rs}
    assert len(rs) == 2 and len(ys) == 1, (rs, ys)


def test_fix2_cap_groups_above_a_part_share_one_row():
    """rp2040-min: the decoupling groups that find no room at their pins stand in ONE row above the MCU (their
    supply symbols on one line) - they were a staircase of four heights."""
    sch, D, _sig = _memory_layout("rp2040-min", "bankrow")
    assert B.BANK_ONE_ROW
    caps = {}
    for s in sch.symbols(include_power=False):
        ns = list(D.pins_of.get(s.ref, {}).values())
        if s.ref.startswith("C") and len(ns) == 2 and any(D.is_gnd(k) for k in ns) and any(
                D.text(k) in ("+3V3", "+1V1") for k in ns):
            caps[s.ref] = round(s.at[1], 2)
    ys = sorted(caps.values())
    top = max(set(ys), key=lambda y: (ys.count(y), -y))        # (the LDO's output cap stands in another frame)
    row = [c for c, y in caps.items() if abs(y - top) < 0.01]
    assert len(row) >= 6, (sorted(row), caps)                 # three groups, one line


def _net_wire_mm(sch, start) -> float:
    """Total length of the wires that are connected (end to end / end on wire) to the point `start`."""
    ws = [((round(w["a"][0], 2), round(w["a"][1], 2)), (round(w["b"][0], 2), round(w["b"][1], 2))) for w in sch.wires()]

    def on(p, w):
        (ax, ay), (bx, by) = w
        return min(ax, bx) - 0.01 <= p[0] <= max(ax, bx) + 0.01 and min(ay, by) - 0.01 <= p[1] <= max(ay, by) + 0.01
    pts, got, grew = [(round(start[0], 2), round(start[1], 2))], set(), True
    while grew:
        grew = False
        for i, w in enumerate(ws):
            if i not in got and (any(on(p, w) for p in pts) or any(on(e, ws[j]) for j in got for e in w)):
                got.add(i)
                pts += list(w)
                grew = True
    return sum(abs(ws[i][0][0] - ws[i][1][0]) + abs(ws[i][0][1] - ws[i][1][1]) for i in got)


def test_fix3_no_perimeter_wire_no_frame_border_name_no_boilerplate():
    """Fix round 3 (docs section 17): (1) stm32-sensor: NRST is named at the switch and at the MCU pin - it ran as a
    wire around the whole crystal - and nothing overlaps there; (2) the name of a net that is wired across two frames
    does not stand on a frame border; (3) no "Place Cx close to Uy" line under the frames."""
    pr, r, _D0, _sig = _laid("stm32-sensor")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    k = next(k for k in D.view.nets if D.text(k) == "NRST")
    sw = next(p for p in D.view.nets[k].pins if p[0].startswith("SW"))
    u = next(p for p in D.view.nets[k].pins if p[0] == "U1")
    # (package B: the reset button now hangs on the wire of the NRST pin, behind the filter cap - one bend down, no
    # wire around the crystal. What must not come back is the PERIMETER wire: 120 mm of it.)
    assert grp[sw] == grp[u], "the reset button is an island again"
    assert _net_wire_mm(sch, xy[sw]) < 70.0, "NRST runs around the crystal again"
    assert r["metrics"]["overlaps"] == 0, r["metrics"].get("overlap_list")
    assert engine.SIDE_SYM_TIGHT and route.LOOSE <= 8 and B.RAIL_LANE >= 10
    wires = [("N", (0.0, 10.16), (40.64, 10.16))]
    ent = ("ADC_IN", "local")
    free = engine._name_on_wire("N", ent, wires, [], [])
    kept = engine._name_on_wire("N", ent, wires, [], [], (), [[free[0] + 2.54, 0.0, 80.0, 30.0]])
    from copper_kicad.sch.model import text_width
    edge = free[0] + 2.54
    assert free[0] - 1.5 < edge < free[0] + text_width(ent[0]) + 1.5          # (the free spot would straddle it)
    assert kept is not None and not (kept[0] - 1.5 < edge < kept[0] + text_width(ent[0]) + 1.5), (free, kept)
    for name in ("stm32-sensor", "li-ion-powerbank"):
        text = O.load_sheet(_laid(name)[0], None, create=False).text()
        assert "close to U" not in text, name


# ───────────────────────── fix round 4 ─────────────────────────
def test_fix4_single_signal_connector_is_wired_on_a_branch():
    """ne555-blinker: the output terminal J2 shares its one signal (PULSE) with the LED chain that already hangs on
    the timer's output pin. It used to float beside that wire with a second PULSE label; now a branch leaves the
    pin's stub and the terminal is wired (dock.TAP): one geometric group, at most one PULSE name."""
    from copper_kicad.sch.pro import dock as DOCK
    assert DOCK.TAP
    pr, r, _sig = _laid_unseen("ne555-blinker")
    assert r["style_used"] == "pro2", r.get("tried")
    sch, D, grp, _xy, _ms, _groups = _after(pr)
    k = D.pins_of["J2"]["1"]
    assert D.text(k) == "PULSE", D.text(k)
    host = [(ref, no) for ref, no in D.view.nets[k].pins if ref == "U1"]
    assert host and grp[("J2", "1")] == grp[host[0]], "J2 is not wired to the timer's output"
    assert sum(1 for l in sch.labels() if l["net"] == "PULSE") <= 1
    assert r["metrics"]["overlaps"] == 0 and not r["metrics"]["hard"], r["metrics"]["hard"]


def test_fix4_page_rank_columns_and_connector_frames():
    """Page composition, fix round 4.
    * rp2040-min: three rank columns (supply left, MCU in the middle, GPIO header in the last column); the debug
      header that was drawn at the MCU is undocked into a frame of its own in an outer column - a block that merely
      HOLDS a connector is no longer tied to the sheet's edge (the right third of the sheet stood empty).
    * audio-amp: the speaker terminal is a column of its own to the right of the amplifier and stands level with it
      (wired straight across the channel), not above it.
    * rs485-node: the sheet's NOTES stand outside every block frame."""
    pr, r, _D0, _sig = _laid("rp2040-min")
    cols = r["page"]["columns"]
    # (class P) the sheet is rows now, not a band of three rank columns: supply input and regulator side by side OVER
    # the MCU frame, the GPIO header in the last column (sheet_fill 0.45 -> 0.51, the outline has the sheet's
    # proportions). The MCU stands in an outer column again, so the debug header stays in its frame.
    assert cols[-1] == ["J3"] and "U1" in cols[0] and len(cols) == 2, cols
    fr_, _s = _frames(pr)
    assert abs(fr_["J1"][1] - fr_["U3"][1]) < 0.01 and fr_["U1"][1] > max(fr_["J1"][3], fr_["U3"][3]), fr_
    assert r["page"]["flow_violations"] == 0
    pr, r, _sig = _laid_unseen("audio-amp")
    cols = r["page"]["columns"]
    assert cols[-1] == ["J3"] and ["U1"] in cols[:-1], cols
    fr, sch = _frames(pr)
    j3, u1 = fr["J3"], fr["U1"]
    assert j3[0] > u1[2] and j3[1] >= u1[1] - 0.01 and j3[3] <= u1[3] + 0.01, (j3, u1)       # beside it, level with it
    sch, D, grp, _xy, _ms, _groups = _after(pr)
    spk = D.pins_of["J3"]["1"]
    other = [(ref, no) for ref, no in D.view.nets[spk].pins if ref != "J3"]
    assert other and grp[("J3", "1")] == grp[other[0]], "the speaker terminal is not wired to the amplifier's output"
    pr, r, _sig = _laid_unseen("rs485-node")
    fr, sch = _frames(pr)
    notes = [n for n in sch.items("text") if str(n[1]).startswith("NOTES")]
    for n in notes:                       # (fix round 5: no NOTES block when all it held were the rails of the title block)
        at = sexpr.find(n, "at")
        x, y = float(at[1]), float(at[2])
        assert not any(f[0] <= x <= f[2] and f[1] <= y <= f[3] for f in fr.values()), ("NOTES inside a frame", x, y)



# ───────────────────────── package A: power and ground conventions ─────────────────────────
def _power_ways(sch) -> dict:
    juncs = [(float(sexpr.find(j, "at")[1]), float(sexpr.find(j, "at")[2])) for j in sch.items("junction")]
    return score.power_ways([(w["a"], w["b"]) for w in sch.wires()], juncs, sch.symbols())


def test_package_a_no_hook_no_ground_above_its_pin():
    """Every supply / ground symbol of the ten bench sheets is reached by a straight wire or ONE L (a top pin that
    is ground: stub, sideways, down). No ground symbol stands above the side pin it serves, no supply symbol below.
    The three symbols that have no upright place lie on their side and are counted."""
    side = 0
    for name in BOARDS:
        pr, r, _D0, _sig = _laid(name)
        m = r["metrics"]
        assert m["power_hooks"] == 0 and m["power_wrong_side"] == 0, (name, m["power_hooks"], m["power_wrong_side"])
        assert _power_ways(O.load_sheet(pr, None, create=False))["power_hooks"] == 0, name
        side += m["power_sideways"]
    assert side <= 3, side
    for name in ("audio-amp", "ne555-blinker", "rs485-node"):
        pr, r, _sig = _laid_unseen(name)
        m = r["metrics"]
        assert r["style_used"] == "pro2", (name, r.get("tried"))
        assert m["power_hooks"] == 0 and m["power_wrong_side"] == 0 and m["power_sideways"] == 0, (name, m["hard"])


def test_package_a_metric_sees_the_old_hooks():
    """With build.SINGLE_L off the arches come back - and power_ways counts them (the metric is not blind)."""
    assert B.SINGLE_L
    B.SINGLE_L = False
    try:
        sch, _D, _sig0 = _memory_layout("li-ion-powerbank", "hooks")
    finally:
        B.SINGLE_L = True
    old = _power_ways(sch)
    assert old["power_hooks"] >= 3 and old["power_wrong_side"] >= 3, old
    sch, D, sig0 = _memory_layout("li-ion-powerbank", "nohooks")
    assert route.verify_partition(_reload(sch), D, sig0) == [], "pre-gate"
    new = _power_ways(sch)
    assert new["power_hooks"] == 0 and new["power_wrong_side"] == 0, new


def test_package_a_pins_of_one_rail_share_a_bar_and_caps_are_wired_to_their_pin():
    """li-ion-powerbank: TEMP and GND of the charger (PROG between them) hang on ONE ground symbol, the two VBUS
    contacts of the receptacle (CC1 / CC2 between them) stand under ONE supply symbol, and the charger's input cap
    is wired to its VCC pin. ne555-blinker: both supply caps hang at the VCC pin - none is given to the reset
    input that is merely strapped to the rail."""
    pr, _r, _D0, _sig = _laid("li-ion-powerbank")
    sch, D, grp, _xy, _ms, _groups = _after(pr)
    assert grp[("U1", "1")] == grp[("U1", "3")], "TEMP and GND of U1 are not on one bar"
    # (class I) the two VBUS contacts of the receptacle used to share ONE bar - which ran across both CC rows right at
    # the pins. Now the CC pull-downs return up to the ground pin's wire and the lower VBUS contact is led out under
    # them to a supply symbol of its own: no supply wire crosses a CC row (test_class_i_*); the bar is gone on purpose
    assert grp[("J1", "B9")] != grp[("J1", "A9")] and not [
        x for x in _r["metrics"]["idiom_crossing_list"] if x.startswith("usbc J1") and "signal" in x]
    vcc = D.pins_of["U1"]["4"]
    c1 = next(no for no, k in D.pins_of["C1"].items() if k == vcc)
    assert grp[("C1", c1)] == grp[("U1", "4")], "C1 is not wired to the VCC pin of U1"
    pr, _r, _sig = _laid_unseen("ne555-blinker")
    sch, D, grp, _xy, _ms, _groups = _after(pr)
    vcc = D.pins_of["U1"]["8"]
    for c in ("C1", "C2"):
        no = next(no for no, k in D.pins_of[c].items() if k == vcc)
        assert grp[(c, no)] == grp[("U1", "8")], f"{c} is not wired to the VCC pin of U1"


def test_package_a_flag_keeps_off_the_supply_symbol():
    """No PWR_FLAG stands closer than 3.81 mm to a supply / ground symbol (it used to sit on the hook right beside
    the ground symbol of the USB-C receptacle), and every net keeps its number of flags."""
    for name in BOARDS:
        pr, r, D0, _sig = _laid(name)
        sch = O.load_sheet(pr, None, create=False)
        fl = [s.pins_world()[0]["xy"] for s in sch.symbols() if s.is_power and O.is_flag(s)]
        pw = [s.pins_world()[0]["xy"] for s in sch.symbols() if s.is_power and not O.is_flag(s)]
        assert len(fl) == sum(ln.flags for ln in D0.view.nets.values()) + sum(o["flags"] for o in D0.view.orphans), name
        for f in fl:
            near = [q for q in pw if abs(q[0] - f[0]) + abs(q[1] - f[1]) < 3.81]
            assert not near, (name, f, near)


def test_package_b_twin_contact_part_is_a_node_part_and_hangs_with_straight_links():
    """Matcher + template, no layout: a 4-pin tactile switch (pins 1 / 2 on the signal, 3 / 4 on ground) is a node
    part of its signal net; the template poses it with one contact of the signal ON the wire, the ground contact
    straight below, and joins either pair tip to tip along its row. A part with three nets is none."""
    pr = _board("rp2040-min", "pkgb-twin")
    sch = O.load_sheet(pr, None, create=False)
    D = Design(sch, export_netlist(pr))
    twins = [r for r in sorted(D.units) if D.npins(r) == 4 and not IM.two_pin(D, r) and IM.node_part(D, r)]
    assert len(twins) >= 2, twins                                   # SW1 (BOOTSEL), SW2 (RUN)
    assert not any(IM.node_part(D, r) for r in D.units if len(set(D.pins_of[r].values())) > 2)
    ref = twins[0]
    N = next(k for k in D.pins_of[ref].values() if not D.is_rail(k))
    assert IM.ends(D, ref, N)[2] != N and D.is_gnd(IM.ends(D, ref, N)[2])
    info = next(i for i in D.blocks if ref in i.sats)
    m = IM.node_match(D, info, N, set(), None)
    assert m is not None and ref in m.refs(), m
    for d in (1, -1):
        T = IP.Template()
        far, _bx, M = IP._hang(T, D, "down0", ref, N, 20.32, 10.16, 1, lead=0, near_role="N", d=d)
        assert D.is_gnd(M) and abs(far[0] - 20.32) < 0.01 and far[1] > 10.16, far     # ground contact straight below
        links = [(role, pts) for role, pts in T.wires]
        assert sorted(role for role, _pts in links) == ["N", "down0"], links
        for _role, (a, b) in links:
            assert abs(a[1] - b[1]) < 0.01 and abs(abs(a[0] - b[0]) - 10.16) < 5.1, (a, b)  # tip to tip, one row
        # the body stands beyond the drop, away from the pin the wire comes from
        other = [q[0] for _r, (a, b) in links for q in (a, b) if abs(q[0] - 20.32) > 0.01]
        assert all((x - 20.32) * d > 0 for x in other), (d, other)


def test_package_b_no_island_parts_hang_on_the_wire_of_their_pin():
    """phone-breathalyzer: the pull-down and the filter cap of ALC_SENSE found no room at PA0 (the divider of the
    next pin stands in their row). They were two fragments joined by the net's name; now they hang on a branch of
    the pin's own wire (dock.dock_nodes). The DNP test points stand ON the wires they probe (dock.dock_points):
    pin tip on a horizontal wire of the net, no hook under the part."""
    assert DOCK.NODE_ON_WIRE
    pr, r, _D0, sig0 = _laid("phone-breathalyzer")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert r["style_used"] == "pro2" and route.signature(sch) == sig0
    k = next(k for k in D.view.nets if D.text(k) == "ALC_SENSE")
    u = next(p for p in D.view.nets[k].pins if D.npins(p[0]) >= 16)
    two = [p for p in D.view.nets[k].pins if D.npins(p[0]) == 2]
    assert len(two) == 2 and all(grp[p] == grp[u] for p in two), [(p, grp[p] == grp[u]) for p in two]
    assert len({round(xy[p][1], 2) for p in two}) == 1, "pull-down and filter cap hang on one row"
    wires = [(w["a"], w["b"]) for w in sch.wires()]
    seen = 0
    for ref in sorted(D.units):
        if D.npins(ref) != 1 or not ref.startswith("TP"):
            continue
        (pn, kk), = D.pins_of[ref].items()
        if D.is_rail(kk) or not any(D.npins(p[0]) >= 16 for p in D.view.nets[kk].pins):
            continue
        seen += 1
        t = xy[(ref, pn)]
        host = next(p for p in D.view.nets[kk].pins if D.npins(p[0]) >= 16)
        assert grp[(ref, pn)] == grp[host], (ref, "not wired to its pin")
        ends = [1 for a, b in wires if abs(a[1] - b[1]) < 0.01 and abs(a[1] - t[1]) < 0.01
                and min(a[0], b[0]) - 0.01 <= t[0] <= max(a[0], b[0]) + 0.01]
        assert ends, (ref, "its tip is not on a horizontal wire")
        assert not any(abs(a[0] - b[0]) < 0.01 and abs(a[0] - t[0]) < 0.01 and (
            abs(a[1] - t[1]) < 0.01 or abs(b[1] - t[1]) < 0.01) for a, b in wires), (ref, "a hook under the test point")
    assert seen >= 2, seen


def test_package_b_wire_between_multi_pin_parts_is_straight_or_named():
    """route.TIGHT_MAX_BENDS / TIGHT_NO_CROSS: a named wire between two parts of three or more pins has at most one
    bend and crosses nothing; otherwise both ends carry the name. stm32-sensor: SWDIO / SWCLK are the other way round
    on the debug header than on the MCU - one is wired straight, the other is two names, they no longer cross.
    motor-driver: the three lines to the programming header are drawn ONE way (they were three parallel doglegs)."""
    assert route.TIGHT_MAX_BENDS == 1 and route.TIGHT_NO_CROSS
    pr, r, _D0, sig0 = _laid("stm32-sensor")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0
    way = {}
    for nm in ("SWDIO", "SWCLK"):
        k = next(k for k in D.view.nets if D.text(k) == nm)
        cp = next(p for p in D.view.nets[k].pins if D.is_conn(p[0]))
        up = next(p for p in D.view.nets[k].pins if D.npins(p[0]) >= 16)
        way[nm] = grp[cp] == grp[up]
        if not way[nm]:
            assert _names(sch, D, k) == 2, (nm, _names(sch, D, k))
    # (class L, bundle.py) the ROUTER still draws no dogleg and no crossing between two multi-pin parts; the pair is
    # now planned as one bundle before the router runs: both lines are wires, drawn one way, and cross once (the
    # floor of the two pin orders) - was: one wired straight, the other two names
    assert sorted(way.values()) == [True, True], way
    assert r["metrics"]["wire_crossings"] <= 3, r["metrics"]["wire_crossings"]
    pr, r, _D0, sig0 = _laid("motor-driver")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    hdr = next(ref for ref in sorted(D.units) if D.is_conn(ref) and D.npins(ref) == 6)
    ways = set()
    for no, k in D.pins_of[hdr].items():
        if D.is_rail(k) or not D.conn(k):
            continue
        ic = [p for p in D.view.nets[k].pins if D.npins(p[0]) == 8]
        if ic and D.text(k).startswith(("AIN", "BIN")):
            ways.add(grp[(hdr, no)] == grp[ic[0]])
    assert len(ways) == 1, ways


from copper_kicad.sch.pro import textbox as TB  # noqa: E402


def _all_ten():
    for name in BOARDS:
        pr, r, _D0, sig0 = _laid(name)
        yield name, pr, r, sig0
    for d in sorted(x.name for x in UNSEEN.iterdir() if x.is_dir()):
        pr, r, sig0 = _laid_unseen(d)
        yield d, pr, r, sig0


def test_package_c_text_collisions_are_zero_on_all_ten():
    """Text hygiene: no Reference / Value / net name / supply value / note stands on another text, a wire, a
    junction, a no-connect cross, a symbol line or a frame border, and no two of them read as one string - measured
    on ink boxes built from KiCad's drawing rules (textbox.collisions), on the seven corpus designs and the three
    unseen ones. The score carries the number as a hard zero."""
    assert "text_collisions" in score.HARD_ZEROS
    for name, pr, r, sig0 in _all_ten():
        assert r["style_used"] == "pro2", (name, r.get("style_used"), r.get("tried"))
        sch = O.load_sheet(pr, None, create=False)
        assert route.signature(sch) == sig0, name
        c = TB.collisions(sch)
        assert c["count"] == 0, (name, c["list"])
        assert r["metrics"]["text_collisions"] == 0, (name, r["metrics"]["text_collision_list"])


def test_package_c_metric_sees_planted_collisions_and_refine_removes_them():
    """The metric is not blind: a Reference pushed onto a wire and a Value pushed onto a net name are each counted;
    TEXT.refine moves exactly such texts and the connectivity of the sheet stays as it was."""
    pr, _r, _D0, _sig0 = _laid("opamp-afe")
    sch = O.load_sheet(pr, None, create=False)
    sig = route.signature(sch)
    assert TB.collisions(sch)["count"] == 0
    sc = TB.scene(sch)
    wire = max((w for w in sc["wires"] if abs(w[1] - w[3]) < 0.01), key=lambda w: abs(w[0] - w[2]))
    lab = next(t for t in sc["texts"] if t["kind"] == "label")
    fields = [t for t in sc["texts"] if t["kind"] == "field" and t["name"] in ("Reference", "Value")
              and len(t["sym"].pins_world()) == 2]
    ref = next(t for t in fields if t["name"] == "Reference")
    val = next(t for t in fields if t["name"] == "Value" and t["sym"] is not ref["sym"])

    def put(t, x, y):
        a = sexpr.find(t["node"], "at")
        a[1], a[2] = round(x, 3), round(y, 3)
    put(ref, (wire[0] + wire[2]) / 2, wire[1])
    put(val, (lab["box"][0] + lab["box"][2]) / 2, (lab["box"][1] + lab["box"][3]) / 2)
    c = TB.collisions(sch)
    assert c["by_kind"].get("wire", 0) >= 1 and c["by_kind"].get("text", 0) >= 1, c
    st = TEXT.refine(sch)
    assert st["before"] == c["count"] and st["after"] == 0 and st["moved"]["field"] >= 2, st
    assert TB.collisions(sch)["count"] == 0 and route.signature(sch) == sig
    # two words on one line with less than a blank and a half between them are one string for the reader
    assert TB.run_together([0, 0, 5, 1.27], [5.9, 0, 9, 1.27]) and not TB.run_together([0, 0, 5, 1.27], [6.6, 0, 9, 1.27])
    assert not TB.run_together([0, 0, 5, 1.27], [5.5, 1.2, 9, 2.47])


def test_package_c_label_never_leaves_an_open_wire_end_or_its_own_wire():
    """A name may slide along the straight wire it sits on and turn to its other side; a name that closes the open
    end of a stub stays (moved inward it would leave a dangling wire end), and no place on a pin or on a point another
    wire touches is offered."""
    wires = [(10.0, 20.0, 20.16, 20.0), (15.08, 20.0, 15.08, 30.0)]
    end = TEXT._label_spots({"at": [20.16, 20.0], "rot": 0}, wires, {(10.0, 20.0)})
    assert end == [((20.16, 20.0), 180)], end
    wires = [(10.0, 20.0, 15.08, 20.0), (15.08, 20.0, 25.24, 20.0), (15.08, 20.0, 15.08, 30.0)]
    mid = TEXT._label_spots({"at": [15.08, 20.0], "rot": 0}, wires, {(10.0, 20.0), (25.24, 20.0)})
    pts = {q for q, _r in mid}
    assert (16.35, 20.0) in pts and (13.81, 20.0) in pts and (15.08, 21.27) in pts, sorted(pts)
    assert (10.0, 20.0) not in pts and (25.24, 20.0) not in pts
    cross = [(10.0, 20.0, 30.0, 20.0), (17.62, 10.0, 17.62, 30.0)]
    pts = {q for q, _r in TEXT._label_spots({"at": [15.08, 20.0], "rot": 0}, cross, set())}
    assert (17.62, 20.0) not in pts and (16.35, 20.0) in pts and (18.89, 20.0) in pts


def test_package_c_no_cross_on_a_hidden_no_connect_pin():
    """DW01A pin 4, MT3608 pin 6, LM4040 pin 3: hidden pins of type no_connect that carried a no-connect flag in
    the source. The cross floated beside the body (there is no pin to see). It is not drawn again; every other flag
    stays where it was; netlist and ERC are judged by the gate and are unchanged."""
    assert GATE.DROP_HIDDEN_NC
    assert GATE.redundant_nc({"hidden": True, "type": "no_connect"})
    assert not GATE.redundant_nc({"hidden": False, "type": "no_connect"})
    assert not GATE.redundant_nc({"hidden": True, "type": "passive"})

    def flagged(s_):
        pts = {(round(float(sexpr.find(n, "at")[1]), 2), round(float(sexpr.find(n, "at")[2]), 2)) for n in s_.items("no_connect")}
        return {(s.ref, p["number"]) for s in s_.symbols(include_power=False) for p in s.pins_world()
                if (round(p["xy"][0], 2), round(p["xy"][1], 2)) in pts}
    for name, parts in (("li-ion-powerbank", {"U2": "4", "U3": "6"}), ("opamp-afe", {"U2": "3"})):
        src = O.load_sheet(_board(name, "nc-src"), None, create=False)
        pr, r, _D0, _sig0 = _laid(name)
        assert r["style_used"] == "pro2" and str(r.get("connectivity", "")).startswith("identical"), (name, r.get("connectivity"))
        sch = O.load_sheet(pr, None, create=False)
        before, after = flagged(src), flagged(sch)
        gone = set(parts.items())
        assert gone <= before and not (gone & after), (name, before, after)
        assert before - gone == after, (name, before - gone, after)
        assert len(sch.items("no_connect")) == len(after), "no cross without a pin under it"
        assert GATE.nc_pins(src) == GATE.nc_pins(sch)


def test_package_c_notes_stand_next_to_their_subject_and_the_sheet_notes_at_the_title_block():
    """A computed note that names a part (LED current, divider) stands next to that part; notes of one kind stay in
    one stack. The NOTES block of the sheet stands left of the title block, not above its top edge, when no frame is
    in the way."""
    assert NOTES.NEAR and engine.NOTES_AT_TITLE_BLOCK
    assert NOTES.subject("D3: I = (5 V - Vf 2 V) / 1k = 3 mA") == ("D3",)
    assert NOTES.subject("R2/C4 low-pass: fc = 1.59 kHz") == ("R2", "C4")
    assert NOTES.subject("NOTES") == () and NOTES.subject("I2C pull-ups R1, R2 = 4.7k to +3V3") == ()
    assert NOTES.is_legend("J2 = 2-pin terminal block: AOUT1, AOUT2") and not NOTES.is_legend("D3: I = 3 mA")
    pr, r, _D0, _sig0 = _laid("motor-driver")
    sch = O.load_sheet(pr, None, create=False)
    notes = {t["text"]: t["box"] for t in TB.scene(sch)["texts"] if t["kind"] == "note"}
    d3 = next(s for s in sch.symbols(include_power=False) if s.ref == "D3").body_bbox()
    nb = next(b for t, b in notes.items() if t.startswith("D3:"))
    gap = max(nb[0] - d3[2], d3[0] - nb[2], nb[1] - d3[3], d3[1] - nb[3], 0.0)
    assert gap <= NOTES.NEAR_MM + 8.0, (gap, nb, d3)
    # (the two motor terminals of one frame; the supply input has a legend of its own in its frame)
    legend = sorted((b[1], b[0]) for t, b in notes.items() if NOTES.is_legend(t) and t[:2] in ("J2", "J3"))
    assert len(legend) == 2 and abs(legend[0][1] - legend[1][1]) < 0.01, legend
    assert abs(legend[1][0] - legend[0][0] - NOTES.NOTE_PITCH) < 0.01, legend
    assert r["notes"]["near"] >= 1
    for name in ("li-ion-powerbank", "opamp-afe", "stm32-sensor"):
        pr, _r, _D0, _sig0 = _laid(name)
        sch = O.load_sheet(pr, None, create=False)
        (_x0, _y0, _x1, _y1), tb = PAGE.area(PAGE.paper_of(sch))
        head = next((t for t in TB.scene(sch)["texts"] if t["kind"] == "note" and t["text"] == "NOTES"), None)
        if head is None:                  # (fix round 5) the rails are in the title block: nothing left for a NOTES block
            cm = [str(e[2]) for e in sexpr.find_all(sexpr.find(sch.tree, "title_block"), "comment")]
            assert any(c.startswith("Rails:") for c in cm), (name, cm)
            continue
        assert head["box"][2] < tb[0] and head["box"][1] >= tb[1] - 0.01, (name, head["box"], tb)


def test_package_c_fields_keep_clear_of_pin_numbers_and_titles():
    """The Reference / Value of a connector stood on the number of its first pin (J2 MOTOR A, J3 SPEAKER, J2 PULSE
    OUT). After the exact pass every field keeps AIR_TEXT to every pin number and AIR_TITLE to a block title."""
    for name, pr, _r, _sig0 in _all_ten():
        sch = O.load_sheet(pr, None, create=False)
        sc = TB.scene(sch)
        nums = [t for t in sc["texts"] if t["kind"] == "pinnum"]
        titles = [t for t in sc["texts"] if t.get("title")]
        for t in sc["texts"]:
            if t["kind"] != "field":
                continue
            assert not any(TB.hit(t["box"], o["box"]) for o in nums), (name, t["owner"], t["text"])
            assert not any(TB.hit(t["box"], o["box"], -TB.AIR_TITLE / 2) for o in titles), (name, t["owner"], t["text"])


# ───────────────────────── package D: page composition ─────────────────────────
def _frame_ink(sch, fr):
    """{block: [x0, y0, x1, y1]} of what is drawn inside each frame (symbols, pins, wires that stay inside, names,
    supply symbols, Reference / Value, notes - not the frame's title), by KiCad's drawing rules (textbox.scene)."""
    sc = TB.scene(sch)
    out = {}
    for k, f in fr.items():
        def inside(b):
            return f[0] <= (b[0] + b[2]) / 2 <= f[2] and f[1] <= (b[1] + b[3]) / 2 <= f[3]
        bx = [t["box"] for t in sc["texts"] if not t.get("title") and inside(t["box"])]
        bx += [[min(g[0], g[2]), min(g[1], g[3]), max(g[0], g[2]), max(g[1], g[3])] for _o, g in sc["segs"]
               if inside([min(g[0], g[2]), min(g[1], g[3]), max(g[0], g[2]), max(g[1], g[3])])]
        bx += [[min(w[0], w[2]), min(w[1], w[3]), max(w[0], w[2]), max(w[1], w[3])] for w in sc["wires"]
               if f[0] <= min(w[0], w[2]) and max(w[0], w[2]) <= f[2] and f[1] <= min(w[1], w[3]) and max(w[1], w[3]) <= f[3]]
        out[k] = [min(b[0] for b in bx), min(b[1] for b in bx), max(b[2] for b in bx), max(b[3] for b in bx)]
    return out


def test_package_d_frames_hug_their_drawings_on_all_ten():
    """Every frame stands 1.2 .. 8 mm from what is drawn in it on every side (under the title strip on top; the
    widest air is under a note stack) - no half-empty frame, nothing on a frame line. Before the package the
    spread was 1.6 .. 61 mm (motor-driver's MCU frame was half empty, the LDO frame of stm32-sensor twice the size
    of its drawing). A frame as wide as its title needs is the exception on the left / right side."""
    for name, pr, r, _sig0 in _all_ten():
        assert r["style_used"] == "pro2", (name, r.get("tried"))
        fr, sch = _frames(pr)
        ink = _frame_ink(sch, fr)
        titles = {}
        for t in TB.scene(sch)["texts"]:
            if t.get("title"):
                for k, f in fr.items():
                    if f[0] <= t["box"][0] <= f[2] and f[1] <= t["box"][1] <= f[1] + engine.TITLE_H:
                        titles[k] = t["box"][2] - t["box"][0]
        for k, f in fr.items():
            c = ink[k]
            pads = (c[0] - f[0], c[1] - f[1] - engine.TITLE_H, f[2] - c[2], f[3] - c[3])
            assert min(pads) >= engine.FIT_MIN, (name, k, pads)
            assert max(pads[1], pads[3]) <= 8.0, (name, k, pads)
            if f[2] - f[0] > titles.get(k, 0.0) + 5.0 + 2.54:
                # (class L: 8.0 -> 9.0. The frames of one column share its width; the protection frame of
                # li-ion-powerbank stands under the wider charger frame, and its drawing lost the name column on its
                # left when BATN became a wire: 8.9 / 7.7 mm of air left / right, on the 1.27 mm grid)
                assert max(pads[0], pads[2]) <= 9.0, (name, k, pads)
        # the report says how the measured drawing was reached
        assert r["page"]["fit"] and any(isinstance(t, dict) and t["taken"] for t in r["page"]["fit"]), (name, r["page"]["fit"])


def test_package_d_smallest_paper_margins_and_title_block():
    """The smallest standard paper the frames fit on (the timer with its supply input: A5, not two small frames on
    A4); frames keep 10 mm to the drawing border and 5 mm to the title block; the drawing stands in the middle of
    the sheet left to right (the margins differ by at most 6 mm) and no lower than the middle."""
    papers = {}
    for name, pr, r, _sig0 in _all_ten():
        fr, sch = _frames(pr)
        papers[name] = sch.paper
        pw, ph = sch.page_size()
        x0, y0 = min(f[0] for f in fr.values()), min(f[1] for f in fr.values())
        x1, y1 = max(f[2] for f in fr.values()), max(f[3] for f in fr.values())
        left, right, top, bottom = x0 - 10.0, pw - 10.0 - x1, y0 - 10.0, ph - 10.0 - y1
        assert min(left, right, top, bottom) >= 10.0, (name, left, top, right, bottom)
        assert abs(left - right) <= 6.0, (name, left, right)
        assert top <= bottom + 0.01, (name, top, bottom)
        tb = [pw - 10.0 - 108.0, ph - 10.0 - 32.0]         # the title block of the default worksheet
        for k, f in fr.items():
            assert f[2] <= tb[0] - 5.0 or f[3] <= tb[1] - 5.0, (name, k, f, tb)
    assert papers["ne555-blinker"] == "A5", papers
    assert papers["rp2040-min"] == "A3" and papers["c3-devboard"] == "A4", papers


def test_package_d_page_rules_unit():
    """page._arrange: no column is stretched, a frame keeps its own width unless it is nearly as wide as its column
    (EVEN), narrow frames share a row with the common gutter, a frame that keeps its own width stands on the side
    in the last column on the right edge when it holds a connector; compose(keep=...) keeps an arrangement; A5
    exists."""
    assert PAGE.PAPERS[0] == "A5" and PAGE.GROW_W == 1.0 and PAGE.GROW_H == 1.0
    W, H = [100.0, 60.0, 96.0], [40.0, 30.0, 30.0]
    a = PAGE._arrange([[0, 1, 2]], W, H, [], "A4")
    assert a["widths"] == [100.0], a["widths"]
    assert a["pos"][1][2] == 60.0 and a["pos"][2][2] == 100.0, a["pos"]          # own width / the column's width
    assert a["pos"][0][0] == a["pos"][1][0] == a["pos"][2][0], a["pos"]          # one left edge
    assert abs((a["pos"][1][1] - a["pos"][0][1] - 40.0) - PAGE.GUT) < 0.01, a["pos"]
    # two narrow frames side by side: the common gutter, not spread over the column
    a = PAGE._arrange([[0, 1, 2]], [150.0, 40.0, 40.0], [40.0, 30.0, 20.0], [], "A4")
    p1, p2 = a["pos"][1], a["pos"][2]
    assert abs(p1[1] - p2[1]) < 0.01 and abs(p2[0] - (p1[0] + p1[2]) - PAGE.GUT) < 0.01, (p1, p2)
    assert p1[3] == 30.0 and p2[3] == 20.0, (p1, p2)                              # each as tall as its drawing
    # the last column: a narrow frame with a connector stands on the sheet's side of its column, one without on the left
    a = PAGE._arrange([[2], [0, 1]], [100.0, 40.0, 50.0], [40.0, 30.0, 30.0], [0], "A4", edge=[0, 1, 0])
    assert abs(a["pos"][1][0] + a["pos"][1][2] - (a["pos"][0][0] + a["pos"][0][2])) < 0.01, a["pos"]
    a = PAGE._arrange([[2], [0, 1]], [100.0, 40.0, 50.0], [40.0, 30.0, 30.0], [0], "A4", edge=[0, 0, 0])
    assert abs(a["pos"][1][0] - a["pos"][0][0]) < 0.01, a["pos"]
    # a small drawing takes the small sheet; the arrangement of an earlier drawing is kept on request
    sizes = {"J1": (40.64, 30.48), "U1": (60.96, 50.8)}
    blocks, sz, F, D = _fake_page(sizes, {"J1": 0, "U1": 1}, {"J1": "first", "U1": None})
    pp, pos, info = PAGE.compose(blocks, sz, F, D)
    assert pp == "A5", pp
    pp, pos, info = PAGE.compose(blocks, sz, F, D, keep=[["J1", "U1"]])
    assert info["columns"] == [["J1", "U1"]], info["columns"]


def test_package_d_supply_chain_stands_above_the_connectors_of_its_column():
    """rp2040-min: USB-C input, then the LDO, then the debug header (was: the header between input and regulator)."""
    pr, r, _D0, _sig0 = _laid("rp2040-min")
    fr, _sch = _frames(pr)
    col = next(c for c in r["page"]["columns"] if "J1" in c)
    ys = {i: fr[i][1] for i in col}
    # (class P: input and regulator share the top row of their column, the MCU stands under them)
    assert ys["J1"] <= ys["U3"] < ys["J2" if "J2" in ys else "U1"], ys


# ───────────────────────── fix round 5 ─────────────────────────
def test_fix5_no_connect_leads_fields_together_rails_once():
    """Fix round 5 (two critics, 7.18 / 10).
    * A no-connect cross that would stand on its own pin's number is drawn half a grid step out, on a lead; when one
      cross of a part needs that, all crosses of the part get it. The gate reads such a cross as the pin's flag
      (gate.nc_marked): the set of flagged pins is the original's, and a second run reads its own drawing back.
    * Reference and Value of a part with pins on every side stand together (were: at two opposite corners).
    * The rails and their sources are written once, in the title block; no free NOTES block repeats them."""
    from copper_kicad.sch.pro import gate, textbox as TB2
    assert B.NC_LEAD and B.FIELDS_TOGETHER and NOTES.RAILS_IN_TITLE_BLOCK and NOTES.BARE_LEGEND_OFF
    leads = 0
    for name, pr, r, _sig0 in _all_ten():
        assert r["style_used"] == "pro2", (name, r.get("tried"))
        sch = O.load_sheet(pr, None, create=False)
        m = r["metrics"]
        assert m["nc_cross_on_own_pin_number"] <= 1, (name, m["nc_cross_on_own_pin_number"])
        tips = {}
        for s in sch.symbols(include_power=False):
            for p in s.pins_world():
                tips[(round(p["xy"][0], 2), round(p["xy"][1], 2))] = (s.ref, p["number"])
        marked = gate.nc_marked(sch)
        assert marked <= set(tips), name
        for n in sch.items("no_connect"):
            a = sexpr.find(n, "at")
            q = (round(float(a[1]), 2), round(float(a[2]), 2))
            if q in tips:
                continue
            ws = [w for w in sch.wires() if q in ((round(w["a"][0], 2), round(w["a"][1], 2)), (round(w["b"][0], 2), round(w["b"][1], 2)))]
            assert len(ws) == 1, (name, q)                 # a cross off a pin ends exactly one lead
            w = ws[0]
            assert abs(abs(w["a"][0] - w["b"][0]) + abs(w["a"][1] - w["b"][1]) - 1.27) < 0.01, (name, w)
            leads += 1
        # together: no part has its Reference above the body and its Value below it
        for s in sch.symbols(include_power=False):
            bb = score.graphic_box(sch, s)
            pos = {str(p[1]): float(sexpr.find(p, "at")[2]) for p in sexpr.find_all(s.node, "property")
                   if str(p[1]) in ("Reference", "Value") and sexpr.find(p, "at") is not None
                   and not B._is_hidden(p)}
            if len(pos) == 2 and len({p["number"] for p in s.pins_world()}) > 2 and s.rot in (0, 180):
                assert not (pos["Reference"] < bb[1] and pos["Value"] > bb[3]), (name, s.ref, pos, bb)
        cm = [str(e[2]) for e in sexpr.find_all(sexpr.find(sch.tree, "title_block"), "comment")]
        assert sum(c.startswith("Rails:") for c in cm) == 1, (name, cm)
        assert not any(str(t_[1]).startswith("NOTES") and ": from " in str(t_[1]) for t_ in sch.items("text")), name
    assert leads >= 20, leads
    # the drawing with leads is read back as the same sheet: a second run changes no flagged pin and stays pro2
    pr = _board("c3-devboard", "fix5")
    sch0 = O.load_sheet(pr, None, create=False)
    nc0 = gate.nc_pins(sch0)
    r1 = engine.relayout(pr, force=True, style="pro2")
    sch1 = O.load_sheet(pr, None, create=False)
    assert r1["style_used"] == "pro2" and gate.nc_pins(sch1) == nc0 and len(nc0) >= 16
    assert Design(sch1, export_netlist(pr)).view.nc == nc0
    r2 = engine.relayout(pr, force=True, style="pro2")
    assert r2["style_used"] in ("pro2", "unchanged"), r2.get("tried")
    assert gate.nc_pins(O.load_sheet(pr, None, create=False)) == nc0
    # a cross at the end of a wire that touches a second wire or a second pin marks nothing
    sch1.add_wire((10.16, 10.16), (12.7, 10.16))
    sch1.add_wire((12.7, 10.16), (12.7, 12.7))
    sch1.add_no_connect((12.7, 10.16))
    assert gate.nc_pins(sch1) == nc0


# ───────────────────────── fix round 6 ─────────────────────────
def test_fix6_reference_and_value_stay_inside_their_frame():
    """(fix round 6, critic round of 6.80) No Reference / Value of a part stands outside the frame the part is drawn in ("BME280 / U2"
    stood in the gutter between two frames), on all ten bench designs; and a pair candidate at a body corner exists."""
    from copper_kicad.sch.pro import text as TEXT
    for name, pr, _r, _sig in _all_ten_and_unseen():
        sch = O.load_sheet(pr, None, create=False)
        sc = TB.scene(sch)
        assert sc["frames"], name
        for t in sc["texts"]:
            if t["kind"] != "field":
                continue
            fr = TEXT._home_frame(TEXT.graphic_box(sch, t["sym"]), sc["frames"])
            assert fr is not None and TEXT._inside(t["box"], fr, 0.0), (name, t["owner"], t["text"], t["box"], fr)
    assert TEXT.IN_FRAME and TEXT.CORNERS
    gb = [0.0, 0.0, 20.0, 30.0]
    sizes = [(5.0, 1.5), (12.0, 1.5)]
    cands = TEXT._candidates(gb, sizes, 0)
    # both lines start on the body's left edge, above the body
    assert any(abs(c[0][0] - 2.5) < 0.01 and abs(c[1][0] - 6.0) < 0.01 and c[1][1] < 0 for c in cands)


def _all_ten_and_unseen():
    yield from _all_ten()
    yield from ((d, pr, r, sig0) for d, pr, r, sig0 in _unseen_laid())


def _unseen_laid():
    for d in sorted(x.name for x in UNSEEN.iterdir() if x.is_dir()):
        pr, r, sig0 = _laid_unseen(d)
        yield d, pr, r, sig0


def test_fix6_part_across_a_pin_pair_is_drawn_across_it():
    """(fix round 6, critic round of 6.80) rs485-node: the 120 R termination between the A and B outputs of the transceiver stands
    upright between the two wires (it lay in line with A and read as a series resistor), each bus net carries its
    name once, and the bus terminal is wired to the transceiver - no pair of names inside one frame."""
    assert B.SPAN and "R" in B.SPAN_PARTS
    pr, r, _sig = _laid_unseen("rs485-node")
    assert r["style_used"] == "pro2", r.get("reason")
    assert "span" in (r["idioms"]["matches"] or {}), r["idioms"]
    sch = O.load_sheet(pr, None, create=False)
    r3 = next(s for s in sch.symbols(include_power=False) if s.ref == "R3")
    tips = [p["xy"] for p in r3.pins_world()]
    assert abs(tips[0][0] - tips[1][0]) < 0.01 and abs(tips[0][1] - tips[1][1]) > 5.0, tips
    names = [str(l["net"]) for l in sch.labels()]
    assert names.count("RS485_A") == 1 and names.count("RS485_B") == 1, names
    D = Design(sch, export_netlist(pr))
    groups = O.geo_groups(sch)
    grp = {tuple(p[:2]): gi for gi, g in enumerate(groups) for p in g["pins"]}
    for u, j in ((("U3", "6"), ("J2", "1")), (("U3", "7"), ("J2", "2"))):
        assert D.pins_of[u[0]][u[1]] == D.pins_of[j[0]][j[1]], (u, j)
        assert grp[u] == grp[j], ("the bus terminal is joined by names again", u, j)


def test_fix6_flag_of_a_signal_net_is_not_an_island():
    """(fix round 6, critic round of 6.80) opamp-afe: the PWR_FLAG of VREF stands on the routed VREF wire (it was parked on a strip of
    its own with a second copy of the name), VREF is ONE wire from the reference to the converter with one name, the
    amplifier's output still reaches the converter by wire, and no flag island is left on any bench design."""
    assert engine.FLAG_HOME and engine.REFIT_BOTH
    pr, r, _D0, _sig = _laid("opamp-afe")
    sch = O.load_sheet(pr, None, create=False)
    names = [str(l["net"]) for l in sch.labels()]
    assert names.count("VREF") == 1, names
    assert names.count("ADC_IN") == 1, names
    assert r["page"]["cross_block_wires"] >= 1, r["page"]
    for name, _pr, r2, _sig0 in _all_ten_and_unseen():
        assert not any(str(h).startswith("flag_islands") for h in (r2["metrics"].get("hard") or [])), (name, r2["metrics"]["hard"])
    # unit: a strip (name - wire - flag) beside a routed run of the same net moves onto the run
    class _D:
        rails = set()
    wires = [("N", (0.0, 0.0), (25.4, 0.0)), ("N", (2.54, 20.32), (10.16, 20.32))]
    lab_strip = ("N", ("VREF", "local"), (2.54, 20.32), 180)
    lab_run = ("N", ("VREF", "local"), (0.0, 0.0), 180)
    labels, flags, fixed = [lab_strip, lab_run], [("N", (10.16, 20.32), "up")], [lab_strip]
    n = engine._flags_home(_D, wires, labels, flags, fixed, [], {}, [])
    assert n == 1 and labels == [lab_run] and len(wires) == 1, (n, labels, wires)
    assert flags[0][0] == "N" and flags[0][1][1] == 0.0 and 2.54 <= flags[0][1][0] <= 22.86, flags
    # ... and stays when the strip carries the net's only name
    wires = [("N", (0.0, 0.0), (25.4, 0.0)), ("N", (2.54, 20.32), (10.16, 20.32))]
    labels, flags, fixed = [lab_strip], [("N", (10.16, 20.32), "up")], [lab_strip]
    assert engine._flags_home(_D, wires, labels, flags, fixed, [], {}, []) == 0 and len(wires) == 2


def test_fix6_ground_text_keeps_off_the_name_column_and_crosses_off_numbers():
    """(fix round 6, critic round of 6.80) The value text of a ground symbol does not stand on a line with a net name closer than
    NAME_GAP ("GND" beside "MISO" in the name column of a header read as the next pin's name), and no no-connect
    cross touches its own pin's number on the corpus designs that had leads on only some pins."""
    assert B.NAME_GAP >= 2.5 and engine.NC_AIR > 0
    for name in ("opamp-afe", "motor-driver", "c3-devboard"):
        pr, r, _D0, _sig = _laid(name)
        sch = O.load_sheet(pr, None, create=False)
        sc = TB.scene(sch)
        labs = [t["box"] for t in sc["texts"] if t["kind"] == "label"]
        for t in sc["texts"]:
            if t["kind"] != "power" or t["text"] != "GND":
                continue
            b = t["box"]
            for u in labs:
                same_line = b[1] + 0.3 < u[3] and u[1] + 0.3 < b[3]
                assert not (same_line and b[0] - 1.5 < u[2] and u[0] - 1.5 < b[2]), (name, b, u)
    for name in ("c3-devboard", "rp2040-min", "opamp-afe"):
        assert _laid(name)[1]["metrics"].get("nc_cross_on_own_pin_number", 0) == 0, name


def test_ship_sch_tidy_defaults_to_pro2_and_explains_a_fallback():
    """Ship step: the TOOL (no style argument) draws pro2; the older styles stay selectable; when the ladder falls
    back the result names the style that was written and why the better ones were refused."""
    import json
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.mcp import registry
    from copper_kicad.sch.pro import tool as TOOL
    t = registry.TOOLS["sch_tidy"]
    assert TOOL.DEFAULT_STYLE == "pro2" and t.schema["properties"]["style"]["enum"] == ["pro", "pro2", "labels"]
    assert "'pro2' (default)" in t.description and "`fallback`" in t.description and len(t.description) <= 1585
    assert "pro2-safe" in registry.HELP["sch_tidy"] and "never redrawn" in registry.HELP["sch_tidy"]   # copper_help has the rest

    def call(tag, **kw):
        pr = _board("c3-devboard", tag)
        out = registry.call_tool("sch_tidy", {"project": str(pr.pro), "preview": False, "force": True, **kw})
        assert not out.get("isError"), out["content"][0]["text"][:400]
        return out["structuredContent"], out["content"][0]["text"]
    d, _txt = call("ship-default")
    assert d["style_used"] == "pro2" and "fallback" not in d and "identical" in d["connectivity"], d
    assert d["verify"]["erc"].get("new", 0) == 0, d["verify"]
    d, _txt = call("ship-pro", style="pro")
    assert d["style_used"] == "pro" and "fallback" not in d, d
    d, _txt = call("ship-plain", style="pro", route="off")           # the first rung of what was asked: no fallback
    assert d["style_used"] == "pro-plain" and "fallback" not in d, d
    d, _txt = call("ship-labels", style="labels")
    assert d["style_used"] == "labels" and "fallback" not in d, d
    orig = B.Blk.enable_v2

    def boom(self):
        raise RuntimeError("idiom layer broken on purpose")
    B.Blk.enable_v2 = boom
    try:
        d, txt = call("ship-fallback")
    finally:
        B.Blk.enable_v2 = orig
    assert d["style_used"] == "pro" and "identical" in d["connectivity"], d
    assert d["fallback"].startswith("asked for pro2, drawn as pro: pro2 refused (") and "broken on purpose" in d["fallback"], d
    assert len(d["fallbacks"]) == 2 and "asked for pro2, drawn as pro" in txt, (d["fallbacks"], txt)
    assert len(json.dumps({k: v for k, v in d.items() if k != "verify"})) < 1700, len(json.dumps(d))


# ───────────────────────── class L: label-only links inside one block (bundle.py) ─────────────────────────
# score.label_only_links per corpus design after the bundle planner (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 20;
# before it: c3 2, li-ion 2, motor 4, opamp 3, breathalyzer 2, rp2040 8, stm32 2). What is left is named there:
# pass-through TVS arrays (c3, breathalyzer, rp2040: two names by the rule of fix round 2), headers whose ground pin
# stands above their signal pins (motor-driver, opamp-afe), NRST on the far side of the stm32.
LINKS_MAX = {"c3-devboard": 2, "li-ion-powerbank": 1, "motor-driver": 4, "opamp-afe": 0, "phone-breathalyzer": 2,
             "rp2040-min": 4, "stm32-sensor": 1}      # (rp2040: 2, plus 2 when the debug header stands in the MCU frame)
#                                                       opamp-afe 3 -> 0: bundle.tuck_leads, dock.PEER_IN_LINE


def test_class_l_track_order_and_crossing_floor():
    """bundle.best_order: two-sided channel routing. The floor is the number of pin pairs whose order differs."""
    from copper_kicad.sch.pro import bundle as BU
    P = BU.P
    # same order on both sides, different pitch: three turning wires, no crossing
    rows = {"a": (0.0, 4 * P), "b": (P, 6 * P), "c": (2 * P, 8 * P)}
    assert BU.floor_crossings(rows) == 0 and BU.best_order(rows)[0] == 0
    t = BU._tracks(BU.best_order(rows)[1], rows)
    assert len(set(t.values())) == 3, t                   # their vertical pieces overlap: one track each
    # an ISP header: MISO, SCK, MOSI at the header against MOSI, MISO, SCK at the MCU - two inverted pairs
    rows = {"MOSI": (0.0, 3 * P), "MISO": (P, P), "SCK": (2 * P, 2 * P)}
    assert BU.floor_crossings(rows) == 2 and BU.best_order(rows)[0] == 2
    # ... and at the offset where every net would have to turn on a row another one needs there is no drawing at all
    # (a cycle of "must turn nearer to the host"): the planner then tries the next offset
    rows = {"MOSI": (0.0, 2 * P), "MISO": (P, 0.0), "SCK": (2 * P, P)}
    assert BU.floor_crossings(rows) == 2 and BU.best_order(rows) is None
    # a net whose host row is another net's peer row must turn nearer to the host (they would share that row)
    rows = {"a": (0.0, P), "b": (P, 2 * P)}
    c, order = BU.best_order(rows)
    assert c == 0 and order == ["a", "b"], (c, order)     # (nearest the PEER first: b turns nearer to the host)
    # a full reversal of four nets needs six crossings: more than a bundle may contain -> two name columns
    rows = {i: (i * P, (3 - i) * P) for i in range(4)}
    assert BU.floor_crossings(rows) == 6 > BU.max_cross(4) == 3
    assert BU.max_cross(6) == 5 and BU.max_cross(2) == 1
    # the exact check of the finished wires: a proper crossing counts, a wire that ends on another is refused
    ok = {"a": [(0.0, 0.0), (10.16, 0.0)], "b": [(5.08, -5.08), (5.08, 5.08)]}
    assert BU._own_crossings(ok) == 1
    bad = {"a": [(0.0, 0.0), (10.16, 0.0)], "b": [(5.08, 0.0), (5.08, 5.08)]}
    assert BU._own_crossings(bad) is None


def test_class_l_flash_and_headers_are_wired_as_bundles():
    """rp2040-min: all six QSPI nets run as wires between the MCU and its flash (they were twelve names), each named
    once, the names in one column at the MCU's pins. stm32-sensor: SWDIO and SWCLK both wired to the debug header
    (the pin orders differ: one crossing). li-ion-powerbank: the protection IC and its dual FET. motor-driver: the
    programming header stays a name column, completely - its ground pin stands above its signal pins, the lead
    would cross all four wires."""
    from copper_kicad.sch.pro import bundle as BU
    assert BU.ENABLED and DOCK.RESERVE
    pr, r, _D0, sig0 = _laid("rp2040-min")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0
    assert r["style_used"] == "pro2" and (r["idioms"]["matches"].get("bundle") or 0) >= 1, r["idioms"]
    flash = next(ref for ref in D.units if sum(1 for k in D.pins_of[ref].values() if D.text(k).startswith("QSPI")) == 6
                 and D.npins(ref) <= 8)
    mcu = next(ref for ref in D.units if D.npins(ref) >= 40)
    lx = []
    for k in [k for k in D.view.nets if D.text(k).startswith("QSPI")]:
        a, b = (flash, _pin_on(D, flash, k)), (mcu, _pin_on(D, mcu, k))
        assert grp[a] == grp[b], (D.text(k), "flash and MCU are not joined by a wire")
        assert _names(sch, D, k) == 1, (D.text(k), _names(sch, D, k))
        lab = next(l for l in sch.labels() if l["net"] == D.text(k))
        assert abs(lab["at"][1] - xy[b][1]) < 0.01, (D.text(k), "the name is not on the row of its MCU pin")
        from copper_kicad.sch.model import text_width
        lx.append(round(lab["at"][0] + (text_width(D.text(k)) if xy[b][0] > lab["at"][0] else 0.0), 0))
    assert max(lx) - min(lx) <= 2.0, lx                   # one column: the names end (or start) at one x
    m = r["metrics"]
    # (the debug header may stand in the MCU frame or in a frame of its own; its two names are not this test)
    assert [x for x in m["label_only_pair_list"] if not x.startswith("J")] == [], m["label_only_pair_list"]
    assert m["wire_crossings"] <= 12, m["wire_crossings"]
    assert m["crossing_near_junction"] == 0 and m["overlaps"] == 0
    pr, r, _D0, sig0 = _laid("stm32-sensor")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0
    for nm in ("SWDIO", "SWCLK"):
        k = next(k for k in D.view.nets if D.text(k) == nm)
        assert _one_piece(D, grp, k) and _names(sch, D, k) == 1, nm
    assert r["metrics"]["wire_crossings"] <= 3, r["metrics"]["wire_crossings"]
    pr, r, _D0, sig0 = _laid("motor-driver")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    hdr = next(ref for ref in D.units if D.is_conn(ref) and D.npins(ref) == 6)
    mcu = next(ref for ref in D.units if not D.is_conn(ref) and all(
        any(D.text(k) == nm for k in D.pins_of[ref].values()) for nm in ("AIN1", "AIN2", "BIN1", "~{RESET}")))
    for nm in ("AIN1", "AIN2", "BIN1", "~{RESET}"):
        k = next(k for k in D.view.nets if D.text(k) == nm)
        # (fix round 7) wired: with the TVS at the input and the LED on the buck's line the sheet fits A4 with the
        # bundle (it used to be un-wired by the paper guard)
        assert grp[(hdr, _pin_on(D, hdr, k))] == grp[(mcu, _pin_on(D, mcu, k))], (nm, "not wired")
    assert r["metrics"]["wire_crossings"] == 3 and r["metrics"]["paper"] == "A4", r["metrics"]
    for name in BOARDS:
        if name in LINKS_MAX:
            m = _laid(name)[1]["metrics"]
            assert m["label_only_links"] <= LINKS_MAX[name], (name, m["label_only_links"], m["label_only_link_list"])
    assert hdr


# links between DIFFERENT parts of one frame that are still names (score.label_only_part_links), all ten designs.
# Before class L: 22 (motor 4, opamp 3, li-ion 1, rp2040 6, stm32 2, rs485 6). What is left is named in section 20 of
# the design document: the programming header of motor-driver (its wired drawing needs one grid step more than A4
# has: engine._paper_guard), the ISP header of rs485-node (RESET lies under the crystal), NRST on the far side of the
# stm32.
# (class P) rp2040-min 0 -> 2: the sheet is rows instead of a band, the MCU frame stands in an outer column and the
# debug header is no longer undocked into a frame of its own - its two names (SWCLK, SWDIO) were names across two
# frames before and are names inside one frame now; the number of names on the sheet is the same (57).
PART_LINKS_MAX = {"motor-driver": 4, "stm32-sensor": 1, "rs485-node": 4, "rp2040-min": 2}
PART_LINKS_BEFORE = 22


def test_class_l_island_pull_ups_are_drawn_at_the_other_pin_of_their_net():
    """engine._rehome: a pull-up that finds no room at the pins of its own IC (rs485-node: the crystal hangs over
    the SDA / SCL rows of the ATmega) is not left beside it as an island with a name. The sensor on the same bus has
    the pins free: both resistors are drawn there, each on the wire of its pin. The netlist is the same, the engine
    reports the move, and with the switch off the islands are back (dock.islands sees them)."""
    assert engine.REHOME
    pr, r, sig0 = _laid_unseen("rs485-node")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert r["style_used"] == "pro2" and route.signature(sch) == sig0
    moved = {x.split(":")[0] for x in r["idioms"]["rehomed"]}
    assert len(moved) == 2, r["idioms"]["rehomed"]
    sensor = next(ref for ref in D.units if D.npins(ref) == 8 and {"SDA", "SCL"} <= {D.text(k) for k in D.pins_of[ref].values()}
                  and D.npins(ref) < 20)
    for ref in sorted(moved):
        (pn, k), = [(pn, k) for pn, k in D.pins_of[ref].items() if not D.is_rail(k)]
        assert D.text(k) in ("SDA", "SCL"), (ref, D.text(k))
        assert grp[(ref, pn)] == grp[(sensor, _pin_on(D, sensor, k))], (ref, "not wired to the sensor's pin")
        assert abs(xy[(ref, pn)][0] - xy[(sensor, _pin_on(D, sensor, k))][0]) <= 30.0, ref
    m = r["metrics"]
    assert m["label_only_part_links"] <= PART_LINKS_MAX["rs485-node"], m["label_only_link_list"]
    assert not any(x.startswith(("SDA(", "SCL(")) for x in m["label_only_link_list"]), m["label_only_link_list"]
    assert m["wire_crossings"] == 0 and m["overlaps"] == 0
    # the switch: the islands are back, and dock.islands names them on the block as it is built
    engine.REHOME = False
    try:
        sch2, D2, sig2 = _memory_layout_unseen("rs485-node", "island")
        assert route.signature(sch2) == sig2
        mcu = next(i for i in D2.blocks if any(D2.npins(a) >= 28 for a in i.anchors))
        blk = B.Blk(D2, mcu)
        blk.enable_v2()
        blk.build()
        isl = {ref for ref, _k in DOCK.islands(blk)}
    finally:
        engine.REHOME = True
    assert moved <= isl, (moved, isl)


def test_class_l_links_between_parts_fell_by_the_bundles_and_the_moves():
    """The acceptance number of class L over the ten bench designs: label-only links between DIFFERENT parts of one
    frame. 22 before; the guard is what the engine reaches (the target of -70 % is NOT reached: what is left needs
    more crossings than wires, see PART_LINKS_MAX). A second pin of ONE part (pass-through TVS) is not counted here
    but stays in label_only_links."""
    total, per = 0, {}
    for name in BOARDS:
        m = _laid(name)[1]["metrics"]
        per[name] = m["label_only_part_links"]
        assert m["label_only_part_links"] <= m["label_only_links"], name
    for name, _pr, r, _sig in _unseen_laid():
        per[name] = r["metrics"]["label_only_part_links"]
    for name, n in per.items():
        assert n <= PART_LINKS_MAX.get(name, 0), (name, n)
        total += n
    assert total <= 11 < PART_LINKS_BEFORE, (total, per)


def test_class_l_header_with_its_ground_above_the_signal_pins_is_wired():
    """opamp-afe: the SPI header's pins are +5V, GND, CS, SCK, MISO. Its ground lead used to end in a symbol on the
    rows of the signal pins (beyond their names), so wires on those rows had to cross it: the header stayed a column
    of names. bundle.tuck_leads leads the ground over the tracks to a symbol beyond them - nothing crosses it - and
    the three nets run as wires (two crossings: the pin orders differ by two pairs). The names stand in one column
    at the converter's pins. dock.PEER_IN_LINE: the reference on the VREF pin (a top pin) is joined by one straight
    wire. Switch bundle.TUCK off: the header is a name column again."""
    from copper_kicad.sch.pro import bundle as BU
    from copper_kicad.sch.model import text_width
    assert BU.TUCK and DOCK.PEER_IN_LINE
    pr, r, _D0, sig0 = _laid("opamp-afe")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert r["style_used"] == "pro2" and route.signature(sch) == sig0
    hdr = next(ref for ref in D.units if D.is_conn(ref) and D.npins(ref) == 6)
    adc = next(ref for ref in D.units if not D.is_conn(ref) and any(D.text(k) == "MISO" for k in D.pins_of[ref].values()))
    lx = []
    for nm in ("~{CS}", "SCK", "MISO"):
        k = next(k for k in D.view.nets if D.text(k) == nm)
        assert grp[(hdr, _pin_on(D, hdr, k))] == grp[(adc, _pin_on(D, adc, k))], (nm, "header and converter not wired")
        assert _names(sch, D, k) == 1, (nm, _names(sch, D, k))
        lab = next(l for l in sch.labels() if l["net"] == nm)
        assert abs(lab["at"][1] - xy[(adc, _pin_on(D, adc, k))][1]) < 0.01, (nm, "the name is not on the row of its pin")
        lx.append(round(lab["at"][0], 1))
    assert max(lx) - min(lx) <= 0.1, lx                    # one column
    m = r["metrics"]
    assert m["wire_crossings"] == 2 and m["crossing_near_junction"] == 0 and m["overlaps"] == 0, m["wire_crossings"]
    assert m["label_only_links"] == 0, m["label_only_link_list"]
    # the header's ground symbol hangs clear of the bundle: no wire of another net passes its lead
    gk = next(k for k in D.pins_of[hdr].values() if D.is_gnd(k))
    gpin = xy[(hdr, _pin_on(D, hdr, gk))]
    lead = [w for w in sch.wires() if abs(w["a"][1] - gpin[1]) < 0.01 and abs(w["b"][1] - gpin[1]) < 0.01
            and min(w["a"][0], w["b"][0]) - 0.01 <= gpin[0] <= max(w["a"][0], w["b"][0]) + 0.01]
    assert len(lead) == 1, lead
    x0, x1 = sorted((lead[0]["a"][0], lead[0]["b"][0]))
    assert x1 - x0 >= 3 * 2.54, (x0, x1)                  # ... over three tracks
    assert not any(abs(w["a"][0] - w["b"][0]) < 0.01 and x0 + 0.01 < w["a"][0] < x1 - 0.01
                   and min(w["a"][1], w["b"][1]) < gpin[1] - 0.01 < max(w["a"][1], w["b"][1]) for w in sch.wires())
    # the reference at the VREF pin
    k = next(k for k in D.view.nets if D.text(k) == "VREF")
    assert _one_piece(D, grp, k), "VREF is not wired in one piece"
    BU.TUCK = False
    try:
        pr2 = _board("opamp-afe", "notuck")
        r2 = engine.relayout(pr2, force=True, style="pro2")
    finally:
        BU.TUCK = True
    assert r2["style_used"] == "pro2" and not r2["idioms"]["matches"].get("bundle"), r2["idioms"]
    assert r2["metrics"]["label_only_part_links"] >= 3 and r2["metrics"]["wire_crossings"] == 0
    assert text_width("x") > 0


def test_class_l_a_bundle_does_not_cost_a_paper_size():
    """engine._paper_guard keeps a sheet on A4: a sheet that needs more than A4 because of its bundles is drawn
    without them when that brings it back to A4. (fix round 7) motor-driver now fits A4 WITH the wired programming
    header (four wires, three crossings), so the guard has nothing to do there; rp2040-min needs more than A4 either
    way and keeps its wires (it is not un-wired to get from A3 to B4)."""
    assert engine.PAPER_GUARD and engine.GUARD_FROM == "A4"
    pr, r, _D0, sig0 = _laid("motor-driver")
    m = r["metrics"]
    assert r["style_used"] == "pro2" and m["paper"] == "A4", m["paper"]
    assert not r["page"].get("paper_guard"), r["page"].get("paper_guard")
    assert r["idioms"]["matches"].get("bundle") == 1, r["idioms"]
    assert m["label_only_part_links"] == 0 and m["wire_crossings"] == 3 and m["crossing_near_junction"] == 0, m
    pr, r, _D0, sig0 = _laid("rp2040-min")
    assert r["metrics"]["paper"] == "A3" and not r["page"].get("paper_guard"), (r["metrics"]["paper"], r["page"].get("paper_guard"))
    assert r["idioms"]["matches"].get("bundle"), r["idioms"]


def test_class_l_switch_restores_the_name_columns():
    """bundle.ENABLED = False draws the sheet as before the planner: the flash is named completely again."""
    from copper_kicad.sch.pro import bundle as BU
    BU.ENABLED = False
    try:
        pr = _board("rp2040-min", "nobundle")
        r = engine.relayout(pr, force=True, style="pro2")
    finally:
        BU.ENABLED = True
    assert r["style_used"] == "pro2" and not r["idioms"]["matches"].get("bundle"), r["idioms"]
    # (class P: 8 + the two names of the debug header, which stands in the MCU frame again)
    assert r["metrics"]["label_only_links"] == 10, r["metrics"]["label_only_links"]
    assert r["metrics"]["wire_crossings"] <= 6, r["metrics"]["wire_crossings"]


# ───────────────────────── class D: decoupling caps belong to a supply pin ─────────────────────────
# before (bench/sch/out/_D_base, bench/sch/decaps.py): 8 caps in 4 groups joined to their IC by a power symbol only
# (rp2040-min C7 - C13 in three groups above the MCU, stm32-sensor C3), 5 repeated supply symbols on the decoupling of
# one IC rail (rp2040-min 4, stm32-sensor 1), three cap heights over the RP2040
def test_class_d_rail_bus_template_is_proven_against_the_netlist():
    """passive.rail_bus_template: risers from every pin of the rail to ONE bar, all caps on that bar, one supply
    symbol, one ground symbol per row; more than BUS_ROW caps make a second row above the first, joined by one wire;
    'RL' deals the caps to both sides. instantiate() refuses a bar that would touch a pin of another net."""
    pr = _board("rp2040-min", "busT")
    sch = O.load_sheet(pr, None, create=False)
    D = Design(sch, export_netlist(pr))
    mb = IM.match_block(D, next(b for b in D.blocks if b.id == "U1"))
    m = next(mm for (a, rail), mm in mb["decap"].items() if a == "U1" and D.text(rail) == "+3V3")
    roles = IP._roles(m, "c")
    assert len(roles) == 10
    pts = [(0.0, 0.0), (2.54, 0.0), (10.16, 0.0)]
    rows = IP.bus_rows(roles, "L")
    assert [len(r) for _s, r in rows] == [5, 5] and all(sd == -1 for sd, _r in rows)
    piece = IMOD.instantiate(D, m, IP.rail_bus_template(D, m, pts, 10, rows))
    assert set(piece["used"]) == set(m.refs())
    ups = [q for _k, q, o, _v in piece["powers"] if o == "up"]
    downs = [q for _k, q, o, _v in piece["powers"] if o == "down"]
    assert len(ups) == 1 and len(downs) == 2, piece["powers"]          # one supply symbol, one ground per row
    ys = sorted({round(si.at[1], 2) for si in piece["syms"]})
    assert len(ys) == 2, ys                                            # two aligned rows
    assert all(si.at[0] < 0 for si in piece["syms"])                   # all left of the pins
    rail = m.nets["RAIL"]
    for x, y in pts:                                                   # every pin is on the bar's wires
        assert any(k == rail and IMOD._on_seg((x, y), a_, b_) for k, a_, b_ in piece["wires"]), (x, y)
    both = IMOD.instantiate(D, m, IP.rail_bus_template(D, m, pts, 10, IP.bus_rows(roles, "RL")))
    xs = [si.at[0] for si in both["syms"]]
    assert sum(1 for x in xs if x > 10.16) == 5 and sum(1 for x in xs if x < 0) == 5
    assert len({round(si.at[1], 2) for si in both["syms"]}) == 1       # one row, half to either side
    assert len([1 for _k, _q, o, _v in both["powers"] if o == "up"]) == 1
    try:                                                               # a pin of another net under the bar's riser
        IMOD.instantiate(D, m, IP.rail_bus_template(D, m, pts, 10, rows), {(2.54, -5.08): m.nets["GND"]})
        raise AssertionError("a riser through a foreign pin was accepted")
    except IMOD.IdiomError:
        pass
    try:
        IP.rail_bus_template(D, m, [(0.0, 0.0), (2.54, 2.54)], 10, rows)
        raise AssertionError("pins on two lines were accepted")
    except IMOD.IdiomError:
        pass


def test_class_d_every_decap_is_wired_to_a_supply_pin_on_all_ten():
    """The acceptance numbers of class D on the ten bench designs: no decoupling cap is tied to its IC by a power
    symbol alone, the decoupling of one IC rail has ONE supply symbol, at most two cap rows stand over one part."""
    for name, _pr, r, _sig in _all_ten():
        m = r["metrics"]
        assert m["decap_detached"] == 0, (name, m["decap_detached_list"])
        assert m["decap_groups_detached"] == 0, name
        assert m["decap_rail_symbol_repeats"] == 0, (name, m["decap_rail_symbol_repeats"])
        assert m["decap_rows_above"] <= 2, (name, m["decap_rows_above"])
        assert m["max_junctions_on_one_power_net"] <= exemplar.MAX_POWER_JUNCTIONS, (name, m["max_junctions_on_one_power_net"])


def test_class_d_rp2040_caps_hang_on_two_rail_bars():
    """rp2040-min: the four +3V3 pins on top of the RP2040 and all ten +3V3 caps are ONE wire group with ONE supply
    symbol, the two +1V1 pins and their three caps another; the caps stand on two lines, the +1V1 row on one of the
    two +3V3 rows (a tier up, over the part: a row beside the pins would reach out past the GPIO names and make the
    frame 7.6 mm wider - one more than the three rank columns of the A3 sheet have room for). Seven of these caps
    were three groups on supply symbols of their own (the metric still sees that drawing with build.RAIL_BUS off)."""
    pr, r, _D0, _sig = _laid("rp2040-min")
    assert r["style_used"] == "pro2" and (r["idioms"]["matches"].get("railbus") or 0) >= 2, r["idioms"]
    sch, D, grp, _xy, _ms, groups = _after(pr)
    at = {s.ref: s.at for s in sch.symbols(include_power=False)}
    rows = {}
    for rail_name, n_caps in (("+3V3", 10), ("+1V1", 3)):
        rail = next(k for k in D.rails if D.text(k) == rail_name)
        pins = [("U1", p) for p, k in D.pins_of["U1"].items() if k == rail]
        assert len({grp[q] for q in pins}) == 1, (rail_name, "the supply pins are not on one bar")
        g = grp[pins[0]]
        caps = [c for c, role in D.role.items() if role.get("anchor") == "U1" and role.get("via") == rail and c.startswith("C")]
        assert len(caps) == n_caps, (rail_name, caps)
        for c in caps:
            assert grp[(c, _pin_on(D, c, rail))] == g, (c, "not on the bar of its rail")
        syms = [s for s in groups[g]["power"] if str(s.value) == rail_name]
        assert len(syms) == 1, (rail_name, len(syms))
        rows[rail_name] = sorted({round(at[c][1], 2) for c in caps})
    assert len(rows["+3V3"]) == 2 and len(rows["+1V1"]) == 1, rows
    assert rows["+1V1"][0] in rows["+3V3"], rows                     # the rows of the two rails share their lines
    assert r["metrics"]["paper"] == "A3", r["metrics"]["paper"]      # the bars did not cost a paper size (class P: the
    #                                                                  sheet is two columns of rows, no longer three rank columns)
    pr2 = _board("rp2040-min", "nobus")
    B.RAIL_BUS = False
    try:
        r2 = engine.relayout(pr2, force=True, style="pro2")
    finally:
        B.RAIL_BUS = True
    assert r2["style_used"] == "pro2" and not r2["idioms"]["matches"].get("railbus"), r2["idioms"]
    assert r2["metrics"]["decap_detached"] >= 6 and r2["metrics"]["decap_rail_symbol_repeats"] >= 3, r2["metrics"]["decap_detached_list"]
    assert r["metrics"]["power_symbols"] <= r2["metrics"]["power_symbols"] - 4


def test_class_d_side_pin_cap_hangs_on_the_rise_of_its_pin():
    """stm32-sensor: the crystal stands in front of the VDD pin on the left of the MCU, so nothing fits on the pin's
    own row. The cap hangs on the bar at the top of the pin's rise, beside the supply symbol - it used to stand alone
    above the part on a supply symbol of its own."""
    pr, r, _D0, _sig = _laid("stm32-sensor")
    sch, D, grp, _xy, _ms, groups = _after(pr)
    rail = next(k for k in D.rails if D.text(k) == "+3V3")
    vdd = [p for p, k in D.pins_of["U1"].items() if k == rail]
    caps = [c for c, role in D.role.items() if role.get("anchor") == "U1" and role.get("via") == rail and c.startswith("C")]
    assert caps and vdd
    for c in caps:
        g = grp[(c, _pin_on(D, c, rail))]
        assert any(grp[("U1", p)] == g for p in vdd), (c, "not wired to a supply pin of U1")
        assert len([s for s in groups[g]["power"] if str(s.value) == "+3V3"]) == 1


# ───────────────────────── class I: idioms (crystal, USB receptacle, regulator feedback) ─────────────────────────
# crossings left inside the crystal / receptacle drawings (score.idiom_metrics), each forced by the pin order of the
# part: a receptacle whose GND pin lies right above its VBUS pin (one ground-over-supply crossing, no signal
# involved), and stm32-sensor's VDDA pin right under the two oscillator rows (its rise crosses both). Before class
# I: c3 3, li-ion 3, rp2040 4, stm32 2 (bench/sch/out/_I_base)
IDIOM_X_MAX = {"c3-devboard": 1, "li-ion-powerbank": 1, "rp2040-min": 1, "stm32-sensor": 2}
IDIOM_OPS = [
    _p("Timer_RTC:DS1307+", "rtc"), _p("Device:Crystal", "y", "32k768"), _p("Device:C", "c1", "12p"), _p("Device:C", "c2", "12p"),
    _p("Connector_Generic:Conn_01x03", "jb", "I2C"),
    _p("Regulator_Switching:LM2596S-5", "bk"), _p("Device:L", "l", "33u"), _p("Device:D_Schottky", "d", "SS34"),
    _p("Device:C", "ci", "100u"), _p("Device:C", "co", "220u"), _p("Connector_Generic:Conn_01x02", "jp", "VIN"),
    _c("X1", "rtc.1", "y.1", "c1.1"), _c("X2", "rtc.2", "y.2", "c2.1"),
    _c("SDA", "rtc.5", "jb.1"), _c("SCL", "rtc.6", "jb.2"), _c("SQW", "rtc.7", "jb.3"),
    _c("+12V", "bk.1", "ci.1", "jp.1"), _c("SWN", "bk.2", "l.1", "d.1"),
    _c("+5V", "l.2", "co.1", "bk.4", "rtc.8", "rtc.3"),
    _c("GND", "rtc.4", "c1.2", "c2.2", "bk.3", "bk.5", "d.2", "ci.2", "co.2", "jp.2"),
    {"op": "pwr_flag", "net": "+12V"}, {"op": "pwr_flag", "net": "GND"},
]


def _idiom_design():
    """A synthetic sheet (stock symbols only): an RTC with a 2-pin crystal and two load caps, and a fixed-output
    buck converter whose feedback pin sits on its own output rail. Built with sch_apply, laid out in pro2."""
    if "idioms" not in _CACHE:
        src = TMP / "idioms-src"
        from copper_kicad.mcp import registry
        import copper_kicad.tools  # noqa: F401
        shutil.rmtree(src, ignore_errors=True)
        src.mkdir(parents=True)
        pr = Project(src, "idioms")
        out = registry.call_tool("sch_apply", {"project": str(pr.pro), "ops": IDIOM_OPS, "preview": False, "layout": "keep"})
        assert not out.get("isError"), out["content"][0]["text"][:1500]
        shutil.rmtree(src / ".copper", ignore_errors=True)
        sch0 = O.load_sheet(pr, None, create=False)
        sig0 = route.signature(sch0)
        D0 = Design(sch0, export_netlist(pr))
        r = engine.relayout(pr, force=True, style="pro2")
        _CACHE["idioms"] = (pr, r, D0, sig0)
    return _CACHE["idioms"]


def _crossings(wires) -> int:
    return sum(1 for i in range(len(wires)) for j in range(i + 1, len(wires))
               if wires[i][0] != wires[j][0] and IMOD._crosses(wires[i][1], wires[i][2], wires[j][1], wires[j][2]))


def test_class_i_synthetic_crystal_is_a_pi_with_one_ground():
    pr, r, D0, sig0 = _idiom_design()
    assert r["style_used"] == "pro2", (r.get("style_used"), r.get("tried"))
    sch, D, grp, xy, ms, groups = _after(pr)
    assert not route.verify_partition(sch, D0, sig0)
    m = r["metrics"]
    assert m["crystals"] == 1 and m["crystal_crossings"] == 0 and m["crystal_grounds_max"] == 1, \
        {k: m[k] for k in ("crystals", "crystal_crossings", "crystal_grounds_max", "idiom_crossing_list")}
    mc = [x for x in ms if x.idiom == "crystal"]
    assert len(mc) == 1, [x.idiom for x in ms]
    mc = mc[0]
    for tag in "AB":
        assert _one_piece(D, grp, mc.nets["N" + tag]), D.text(mc.nets["N" + tag])     # wired, no name island
    # symmetric: both load caps at one height, left and right of the crystal, their ground pins on one bar
    ca, cb, y = (D.units[mc.roles[t]][0] for t in ("CA", "CB", "Y"))
    assert abs(ca.at[1] - cb.at[1]) < 0.01, (ca.at, cb.at)
    assert min(ca.at[0], cb.at[0]) < y.at[0] < max(ca.at[0], cb.at[0]), (ca.at, y.at, cb.at)
    gp = {grp[(r_, p)] for r_ in (ca.ref, cb.ref) for p, k in D.pins_of[r_].items() if D.is_gnd(k)}
    assert len(gp) == 1 and len(groups[next(iter(gp))]["power"]) == 1


def test_class_i_feedback_pin_on_the_output_rail_is_wired_to_the_output_node():
    pr, r, _D0, _sig0 = _idiom_design()
    sch, D, grp, xy, ms, groups = _after(pr)
    mp = [x for x in ms if x.idiom == "buck"]
    assert len(mp) == 1 and mp[0].extra.get("sense") is not None, [(x.idiom, x.extra) for x in ms]
    u, fb = mp[0].host[0], mp[0].extra["sense"]
    ind = mp[0].roles["L"]
    lp = next(p for p, k in D.pins_of[ind].items() if k == mp[0].nets["VOUT"])
    assert grp[(u, fb)] == grp[(ind, lp)], "the feedback pin is not on the wire of the output node"
    assert r["metrics"]["sense_hidden"] == 0 and r["metrics"]["idiom_crossings"] == 0, r["metrics"]["idiom_crossing_list"]
    # the same on the bench: the LM2596S-5 of motor-driver; and the metric sees the supply symbol the pin used to get
    assert _laid("motor-driver")[1]["metrics"]["sense_hidden"] == 0
    keep = ST.SENSE_WIRE
    ST.SENSE_WIRE = False
    try:
        pr2 = _board("motor-driver", "nosense")
        r2 = engine.relayout(pr2, force=True, style="pro2")
    finally:
        ST.SENSE_WIRE = keep
    assert r2["style_used"] == "pro2" and r2["metrics"]["sense_hidden"] == 1, r2["metrics"].get("sense_hidden_list")


def test_class_i_cc_pull_downs_return_without_crossing_a_row():
    """Template level, on two receptacle symbols (D+ / D- between the CC rows; CC rows next to each other), both
    directions: the returns rise to the ground pin's own wire, nothing crosses."""
    seen = set()
    for name in ("c3-devboard", "li-ion-powerbank", "rp2040-min"):
        pr = _board(name, "cc")
        D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
        for info in D.blocks:
            for j, m in ST.match_block(D, info, set())["usbc"].items():
                si = D.units[j][0]
                plan = ST.cc_plan(D, si, m)
                assert plan.get("g") is not None, (name, plan)
                for s in (1, -1):
                    B.set_pose(si, 0.0, 0.0, 0, None if s > 0 else "y")
                    ps = B.pins(si)
                    if tuple(ps[m.extra["pins"]["0"]]["out"]) != (s, 0):
                        B.set_pose(si, 0.0, 0.0, 0, "y" if s > 0 else None)
                        ps = B.pins(si)
                    tips = {t: tuple(ps[no]["xy"]) for t, no in m.extra["pins"].items()}
                    ctx = {tuple(q["xy"]): D.pins_of[j].get(no) for no, q in ps.items()}
                    v = ps[plan["v"]] if plan.get("v") is not None else None
                    if v is not None:
                        m.nets["VB"] = D.pins_of[j][plan["v"]]
                    piece = None
                    for stag in (True, False):
                        for k in (0, 2, 4, 8):
                            try:
                                piece = IMOD.instantiate(D, m, ST.cc_template(
                                    D, m, tips, s, k, stag, tuple(ps[plan["g"]]["xy"]), plan["mode"],
                                    tuple(v["xy"]) if v is not None else None), ctx)
                                break
                            except IMOD.IdiomError:
                                continue
                        if piece is not None:
                            break
                    m.nets.pop("VB", None)
                    assert piece is not None, (name, s, plan)
                    assert _crossings(piece["wires"]) == 0, (name, s)
                    assert all(o in ("up", "down") for _n, _q, o, _v in piece["powers"])
                    seen.add((D.comps[j]["lib_id"], plan["mode"], s))
        # ... and on the finished sheet: no wire of the receptacle's supply / ground / CC nets crosses a signal
        lst = _laid(name)[1]["metrics"]["idiom_crossing_list"]
        assert not [x for x in lst if x.startswith("usbc J") and "signal" in x], (name, lst)
    assert {x[1] for x in seen} == {"split", "up"} and len({x[0] for x in seen}) >= 2, seen


def test_class_i_diagonal_crystal_steps_around_its_case_pin():
    """A 4-pin crystal whose case pins are diagonal to its signal pins (rp2040-min): the plain drawing crosses once,
    the `jog` drawing does not - with either side towards the IC."""
    pr = _board("rp2040-min", "xtal")
    D = Design(O.load_sheet(pr, None, create=False), export_netlist(pr))
    n = 0
    for info in D.blocks:
        for m in ST.match_block(D, info, set())["crystal"]:
            if len(D.pins_of[m.roles["Y"]]) != 4:
                continue
            for s in (1, -1):
                got = {}
                for jog in (False, True):
                    for mir in (None, "x"):
                        for e in (4, 10, 12):
                            try:
                                piece = IMOD.instantiate(D, m, ST.crystal_template(
                                    D, m, {"A": (0.0, 0.0), "B": (0.0, 10.16)}, s, 8, e * B.U, mir, 0, jog))
                            except IMOD.IdiomError:
                                continue
                            c = _crossings(piece["wires"])
                            got[jog] = min(got.get(jog, 9), c)
                            assert len(piece["powers"]) <= 2
                assert got.get(False) == 1 and got.get(True) == 0, (s, got)
                n += 1
    assert n == 2, n
    assert _laid("rp2040-min")[1]["metrics"]["crystal_crossings"] == 0


def test_class_i_bench_numbers():
    for name in BOARDS:
        m = _laid(name)[1]["metrics"]
        assert m["idiom_crossings"] <= IDIOM_X_MAX.get(name, 0), (name, m["idiom_crossings"], m["idiom_crossing_list"])
        assert m["sense_hidden"] == 0, (name, m["sense_hidden_list"])
        assert m["crystal_grounds_max"] <= 2, name
    for name in ("audio-amp", "ne555-blinker", "rs485-node"):
        m = _laid_unseen(name)[1]["metrics"]
        assert m["idiom_crossings"] == 0 and m["sense_hidden"] == 0, (name, m["idiom_crossing_list"])


def test_class_p_page_rules_unit():
    """page.py, class P: a channel takes the sheet's spare width up to CH_EXTRA_MAX; packed rows (a row takes the
    next frame whenever the column is wide enough) are an alternative the composer may take; a column over the title
    block counts with the title block under it; score.page_use reads fill, span and empty thirds off the frames."""
    assert PAGE.CH_EXTRA_MAX > 0 and PAGE.PACK
    a = PAGE._arrange([[0], [1]], [60.96, 60.96], [40.64, 40.64], [0], "A4")
    assert abs((a["channels"][0][1] - a["channels"][0][0]) - (PAGE.GUT + PAGE.CH_EXTRA_MAX)) < 0.01, a["channels"]
    a = PAGE._arrange([[0], [1]], [124.46, 124.46], [40.64, 40.64], [0], "A4")       # no spare width: the least channel
    assert abs((a["channels"][0][1] - a["channels"][0][0]) - PAGE.GUT) < 0.01, a["channels"]
    W, H = [99.06, 15.24, 121.92], [30.48, 30.48, 60.96]
    strict = PAGE._arrange([[0, 1, 2]], W, H, [], "A4")
    packed = PAGE._arrange([[0, 1, 2]], W, H, [], "A4", pack=True)
    assert strict["rows"] == [[[0], [1], [2]]] and packed["rows"] == [[[0, 1], [2]]], (strict["rows"], packed["rows"])
    assert packed["hall"] < strict["hall"] and packed["widths"] == [121.92]
    p0, p1 = packed["pos"][0], packed["pos"][1]
    assert abs(p0[1] - p1[1]) < 0.01 and p1[0] >= p0[0] + p0[2] + PAGE.GUT - 0.01, (p0, p1)
    # a column that ends over the title block: what is under it is not a hole
    a = PAGE._arrange([[0], [1]], [121.92, 121.92], [149.86, 121.92], [0], "A4")
    assert a is not None and a["colh_tb"][1] > a["colh"][1] and a["colh_tb"][0] == a["colh"][0], a
    # in a column a frame stands above the frame it drives (was: wherever the wires to the next column pulled it)
    sizes = {"J1": (40.64, 30.48), "J2": (40.64, 30.48), "U1": (60.96, 50.8), "J3": (40.64, 30.48)}
    blocks, sz, F, D = _fake_page(sizes, {"J1": 0, "J2": 0, "U1": 1, "J3": 2}, {"J1": "first", "J2": None, "U1": None, "J3": "last"})
    pp, pos, info = PAGE.compose(blocks, sz, F, D)
    assert pp == "A5" and sorted(i for c in info["columns"] for i in c) == sorted(sizes), (pp, info["columns"])
    m = score.page_use(297.0, 210.0, [[20.32, 20.32, 276.86, 99.06]], [[20.32, 20.32, 276.86, 99.06]])
    assert m["page_span"][0] > 0.99 and m["page_empty_thirds"] == 0 and 0.45 < m["page_fill"] < 0.55, m   # (title block below)
    m = score.page_use(420.0, 297.0, [[20.32, 20.32, 276.86, 99.06]], [[20.32, 20.32, 276.86, 99.06]])
    assert m["page_empty_thirds"] == 1, m                 # the middle level third of an A3 sheet holds nothing


# (class P) sheet_fill after the page rules of class P, a little under the measured value (bench/sch/out/_P_final).
# NOT reached, and why: phone-breathalyzer (A3: its frames miss A4 by 7 mm beside the title block), audio-amp (A4: the
# A5 drawing exists, but the name WIPER then stands across a frame border and the refit refuses it), ne555-blinker
# (A5, the smallest sheet: the title block takes a quarter of it)
# (fix round 7) the ISO B sizes: phone-breathalyzer on B4 (0.44 -> 0.61), audio-amp on B5 (0.22 -> 0.33). motor-driver
# 0.57 -> 0.47: its frames are smaller (the TVS hangs on the input's wire, the LED on the buck's line) and stand in two
# rows of two on the same A4 sheet, with the programming header wired
P_FILL = {"c3-devboard": 0.45, "li-ion-powerbank": 0.62, "motor-driver": 0.46, "opamp-afe": 0.55,
          "phone-breathalyzer": 0.59, "rp2040-min": 0.49, "stm32-sensor": 0.56,
          "audio-amp": 0.31, "ne555-blinker": 0.33, "rs485-node": 0.62}
P_PAPER = {"phone-breathalyzer": "B4", "rp2040-min": "A3", "ne555-blinker": "A5", "audio-amp": "B5"}


def test_class_p_bench_numbers():
    """The sheet is used: fill per design, no third of the drawable area empty, no paper beyond A4 except the two
    designs whose frames do not fit it, the outline of rp2040-min is rows (not a band), and the smaller sheet never
    costs a readable name (engine.REFIT_KEEPS_TEXT)."""
    rows = [(n, _laid(n)[1]) for n in BOARDS] + [(n, _laid_unseen(n)[1]) for n in ("audio-amp", "ne555-blinker", "rs485-node")]
    good = 0
    for name, r in rows:
        m = r["metrics"]
        assert r["style_used"] == "pro2", (name, r.get("tried"))
        assert m["sheet_fill"] >= P_FILL[name], (name, m["sheet_fill"])
        assert m["page_empty_thirds"] == 0, (name, m["page_span"])
        assert m["paper"] == P_PAPER.get(name, "A4"), (name, m["paper"])
        assert m["text_collisions"] == 0, (name, m["text_collision_list"])
        assert m["flow_violations"] == 0 and m["connector_edge_ratio"] == 1.0, name
        good += m["sheet_fill"] >= 0.45
    assert good >= 7, good                                # (target 8 of 10: see the comment at P_FILL)
    m = dict(rows)["rp2040-min"]["metrics"]
    assert m["page_span"][1] >= 0.75 and m["page_fill"] >= 0.6, (m["page_span"], m["page_fill"])


def test_fix7_b_sizes_input_protection_and_rail_led():
    """Fix round 7. (a) A sheet between two A sizes is written as a user size with the ISO B dimensions and read back
    by its name. (b) motor-driver: the TVS behind the input fuse hangs on the +12V wire of the supply input (one wire
    group with the fuse), the power LED chain hangs on the buck's output line - neither is an island of supply
    symbols. (c) the Reference / Value of a large part do not stand beside the lead of a bottom pin when a corner over
    the body is free (rs485-node U2)."""
    pr, r, _D0, sig0 = _laid("phone-breathalyzer")
    sch = O.load_sheet(pr, None, create=False)
    assert sch.paper == "User" and sch.page_size() == (353.0, 250.0) and PAGE.paper_of(sch) == "B4" == r["metrics"]["paper"]
    assert route.signature(sch) == sig0
    pr, r, _D0, sig0 = _laid("motor-driver")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0
    assert D.block_of["D1"] == D.block_of["J1"] == D.block_of["F1"], (D.block_of["D1"], D.block_of["J1"])
    k12 = next(k for k in D.view.nets if D.text(k) == "+12V")
    assert grp[("D1", _pin_on(D, "D1", k12))] == grp[("F1", _pin_on(D, "F1", k12))], "the TVS is not on the fuse's wire"
    k5 = next(k for k in D.view.nets if D.text(k) == "+5V")
    assert grp[("R6", _pin_on(D, "R6", k5))] == grp[("L1", _pin_on(D, "L1", k5))], "the power LED is not on the output line"
    assert r["metrics"]["text_collisions"] == 0 and r["metrics"]["sense_hidden"] == 0
    pr, r, sig0 = _laid_unseen("rs485-node")
    sch = O.load_sheet(pr, None, create=False)
    u2 = next(s_ for s_ in sch.symbols(include_power=False) if s_.ref == "U2")
    gb = TEXT.graphic_box(sch, u2)
    assert not TEXT._at_bottom_pin(u2, gb, [TEXT.field_box(p, u2) for p in TEXT.visible_fields(u2)])


# ───────────────────────── fix round 8 (critics 7.53): comb bundles, crosses in line, frames on the top line ─────────────────────────
def _wire_path_bends(sch, a, b):
    """Bends of the shortest wire path from point a to point b (None when the two are not joined by wires)."""
    ws = [((round(w["a"][0], 2), round(w["a"][1], 2)), (round(w["b"][0], 2), round(w["b"][1], 2))) for w in sch.wires()]
    ends = {e for w in ws for e in w} | {(round(a[0], 2), round(a[1], 2)), (round(b[0], 2), round(b[1], 2))}
    adj: dict = {}
    for (p, q) in ws:
        on = sorted((e for e in ends if min(p[0], q[0]) - 0.01 <= e[0] <= max(p[0], q[0]) + 0.01
                     and min(p[1], q[1]) - 0.01 <= e[1] <= max(p[1], q[1]) + 0.01), key=lambda e: (e[0], e[1]))
        for u, v in zip(on, on[1:]):
            adj.setdefault(u, []).append(v)
            adj.setdefault(v, []).append(u)
    a, b = (round(a[0], 2), round(a[1], 2)), (round(b[0], 2), round(b[1], 2))
    best = {(a, None): 0}
    todo = [(0, a, None)]
    out = None
    while todo:
        todo.sort()
        n, u, d = todo.pop(0)
        if u == b:
            out = n if out is None else min(out, n)
            continue
        for v in adj.get(u, ()):
            d2 = "h" if abs(u[1] - v[1]) < 0.01 else "v"
            n2 = n + (1 if d is not None and d2 != d else 0)
            if best.get((v, d2), 99) > n2:
                best[(v, d2)] = n2
                todo.append((n2, v, d2))
    return out


def test_fix8_far_side_pins_of_a_flash_are_combed_not_wrapped():
    """rp2040-min: the three QSPI pins on the far side of the flash were led around the part on nested loops and
    back under it to their tracks (four bends each, the worst spaghetti of the critic round). With bundle.COMB the
    flash stands over the rows of those nets: each runs on under the part and turns once into its pin - no wire of
    the bus has more than two bends. The boot switch hangs on the chip-select track running on (dock.TAP_TRACK),
    not beside the flash with a name of its own."""
    from copper_kicad.sch.pro import bundle as BU
    assert BU.COMB and DOCK.TAP_TRACK
    pr, r, _D0, sig0 = _laid("rp2040-min")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0
    flash = next(ref for ref in D.units if sum(1 for k in D.pins_of[ref].values() if D.text(k).startswith("QSPI")) == 6
                 and D.npins(ref) <= 8)
    mcu = next(ref for ref in D.units if D.npins(ref) >= 40)
    for k in [k for k in D.view.nets if D.text(k).startswith("QSPI")]:
        n = _wire_path_bends(sch, xy[(flash, _pin_on(D, flash, k))], xy[(mcu, _pin_on(D, mcu, k))])
        assert n is not None and n <= 2, (D.text(k), n)
    ss = next(k for k in D.view.nets if D.text(k) == "QSPI_SS")
    assert _one_piece(D, grp, ss) and _names(sch, D, ss) == 1, "the boot resistor is not on the chip-select wire"
    # the planner itself: with the comb off the same bundle needs the wrap rows again
    old = BU.COMB
    BU.COMB = False
    try:
        sch2, D2, _s = _memory_layout("rp2040-min", "nocomb")
    finally:
        BU.COMB = old
    xy2 = {(s.ref, p["number"]): tuple(p["xy"]) for s in sch2.symbols(include_power=False) for p in s.pins_world()}
    worst = max(_wire_path_bends(sch2, xy2[(flash, _pin_on(D2, flash, k))], xy2[(mcu, _pin_on(D2, mcu, k))]) or 0
                for k in D2.view.nets if D2.text(k).startswith("QSPI"))
    assert worst >= 4, worst


def test_fix8_crosses_in_one_line_pinout_notes_and_frames_on_the_top_line():
    """(a) stm32-sensor: the no-connect crosses of one side of the MCU stand in one line - a Value that stood in the
    way of one lead (C5 beside PA2) is re-seated by the text solver instead of stopping the lead (engine.NC_ONE_LINE).
    (b) A generic 1xN header of three or more pins gets its pinout note (opamp-afe J3 SPI, motor-driver J4 PROG: the
    pin order could only be read off the crossing wires). (c) A frame that has its column to itself is only moved
    level with rows it can be WIRED to: the GPIO header of c3-devboard (a bus of names) and the sensor of
    stm32-sensor (95 mm away) stand on the common top line of the frames."""
    assert engine.NC_ONE_LINE and engine.SETTLE_WIRED_ONLY
    from copper_kicad.sch.pro import notes as NOTES
    assert NOTES.GENERIC_PINOUT
    pr, r, _D0, sig0 = _laid("stm32-sensor")
    sch, D, _grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0 and r["metrics"]["text_collisions"] == 0
    mcu = max(D.units, key=lambda ref: D.npins(ref))
    si = D.units[mcu][0]
    ncs = [(float(sexpr.find(n, "at")[1]), float(sexpr.find(n, "at")[2])) for n in sch.items("no_connect")]
    left = [p for p in si.pins_world() if tuple(p["out"]) == (-1, 0) and (mcu, p["number"]) in D.view.nc]
    assert len(left) >= 3
    xs = set()
    for p in left:
        on_row = [q for q in ncs if abs(q[1] - p["xy"][1]) < 0.01 and -0.01 <= p["xy"][0] - q[0] < 3.0]
        assert on_row, p["number"]
        xs.add(round(on_row[0][0], 2))
    assert len(xs) == 1, xs
    for name, value in (("opamp-afe", "SPI"), ("motor-driver", "PROG")):
        pr, r, _D0, _sig = _laid(name)
        sch = O.load_sheet(pr, None, create=False)
        D = Design(sch, export_netlist(pr))
        j = next(ref for ref in D.units if D.is_conn(ref) and str(D.comps[ref]["value"]) == value)
        texts = [t["text"] for t in TB.scene(sch)["texts"] if t["kind"] == "note"]
        assert any(t.startswith(f"{j} = 1x6 header: ") for t in texts), (name, texts)
    for name in ("c3-devboard", "stm32-sensor"):
        pr, r, _D0, _sig = _laid(name)
        fr, _sch = _frames(pr)
        last = max(fr.values(), key=lambda f: f[0])
        assert abs(last[1] - min(f[1] for f in fr.values())) < 0.01, (name, fr)


def test_fix8_pull_up_on_a_bundle_wire_lies_in_line_beyond_the_turn():
    """opamp-afe: the pull-up of the converter's chip select waited for the SPI bundle and was then hung on a branch
    BELOW the wire - entered from underneath, a hairpin (right, down, back left, up). It cannot stand over the wire
    (SCK and MISO run there), so it lies in line with the row running on beyond the turn, its supply symbol upright
    at the far end (dock.TAP_PULL): the pin on the net is level with the converter's pin, one straight piece."""
    assert DOCK.TAP_PULL
    pr, r, _D0, sig0 = _laid("opamp-afe")
    sch, D, grp, xy, _ms, _groups = _after(pr)
    assert route.signature(sch) == sig0
    cs = next(k for k in D.view.nets if "CS" in D.text(k))
    adc = next(ref for ref, _no in D.view.nets[cs].pins if not D.is_conn(ref) and D.npins(ref) > 2)
    rp = next(ref for ref, _no in D.view.nets[cs].pins if D.npins(ref) == 2)
    a, b = xy[(adc, _pin_on(D, adc, cs))], xy[(rp, _pin_on(D, rp, cs))]
    assert abs(a[1] - b[1]) < 0.01, (a, b)
    assert _wire_path_bends(sch, a, b) == 0, _wire_path_bends(sch, a, b)
    ups = [s for s in sch.symbols() if s.is_power and s.value == "+5V"]
    assert all(s.rot == 0 for s in ups), [(s.at, s.rot) for s in ups]
    assert r["metrics"]["wire_crossings"] <= 2 and not r["metrics"]["hard"], r["metrics"]


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
