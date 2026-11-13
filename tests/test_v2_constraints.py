"""Router v2 electrical constraints (design §14): the model, detection from KiCad rules / net classes / net
names (with the false positives that must NOT be detected), length measurement equal to KiCad's DRC, KiCad's
constraint markers, and the report.

The length figures asserted here are the ones KiCad 10.0 printed for the same geometry (bench/route/cons_probe.py,
design §14.9, and the engineer-A probe recorded in docs/internal/v2-interface-notes.md).

Run:  python tests/test_v2_constraints.py      (pure Python; one test reads three corpus boards through KiCad's
                                                Python when bench/route/corpus exists)
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad.pcb.guided import model, plan as P  # noqa: E402
from copper_kicad.pcb.v2 import constraints as C  # noqa: E402
from copper_kicad.pcb.v2.types import ConnV2, Ctx, Piece, Terminal  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "bench" / "route" / "corpus"


# ── a synthetic board (no KiCad) ──────────────────────────────────────────
def board(pads, classes=None, layers=("F.Cu", "B.Cu"), tracks=(), vias=()):
    """pads: [(ref, num, net, x, y)] or with (hw, hh, kind) appended. classes: {net: {'class', 'dp_gap', ...}}."""
    names = sorted({p[2] for p in pads if p[2]})
    df = {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.2, "dp_width": 0.2}
    ex = {"layers": list(layers),
          "rules": {"clearance": 0.15, "track_width": 0.15, "via": 0.6, "drill": 0.3, "edge_clearance": 0.3,
                    "hole_clearance": 0.25, "hole_to_hole": 0.25, "annular": 0.1, "default": dict(df)},
          "nets": {n: {"code": i + 1, "class": "Default", **df, **((classes or {}).get(n) or {})}
                   for i, n in enumerate(names)},
          "outline": {"closed": True, "polys": [{"o": [[0, 0], [60, 0], [60, 60], [0, 60]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [list(t) for t in tracks],
          "vias": [list(v) for v in vias], "zones": [], "mask_openings": [],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    allL = list(range(len(layers)))
    for i, p in enumerate(pads):
        ref, num, net, x, y = p[:5]
        hw, hh, kind = (p[5:] + (0.6, 0.3, "smd")[len(p) - 5:]) if len(p) > 5 else (0.6, 0.3, "smd")
        lays = allL if kind == "tht" else [0]
        d = {"ref": ref, "num": str(num), "net": net, "x": x, "y": y, "kind": kind, "layers": lays, "c": i + 1,
             "shapes": [[lays, "box", [x, y, 1.0, 0.0, hw, hh, 0.0]]]}
        if kind == "tht":
            d["hole"] = [x, y, 1.0, 0.0, 0.25, 0.25]
        ex["pads"].append(d)
    return model.Board(ex)


def two_parts(nets, pitch=1.0, x1=5.0, x2=35.0, y0=4.0, ref1="J1", ref2="U1", reverse=False):
    """Two parts facing each other, one pad per net on each, in the given order."""
    pads = []
    for i, n in enumerate(nets):
        pads.append((ref1, i + 1, n, x1, y0 + i * pitch))
        j = len(nets) - 1 - i if reverse else i
        pads.append((ref2, j + 1, n, x2, y0 + j * pitch))
    return pads


def resolve(b, plan=None, dru="", pro_classes=None):
    cfg = P.resolve(b, plan)
    proj = C.ProjectRules(rules=C.parse_dru(dru), classes=dict(pro_classes or {}), stack=C.Stackup.default(b.L))
    return C.resolve(b, cfg, cfg.x, proj), cfg


class State:
    """What constraints.measure needs of a RouteState."""

    def __init__(self, pieces=(), status=None):
        self._p = list(pieces)
        self._s = status or {}

    def pieces(self, conn=None, net=None):
        return [p for p in self._p if net is None or p.net == net]

    def status(self, idx):
        return self._s.get(idx, "routed")


def seg(net, pts, layer=0, w=0.2, first=1):
    return [Piece(id=first + i, net=net, conn=-1, kind="seg", layer=layer, x1=a[0], y1=a[1], x2=b[0], y2=b[1], w=w)
            for i, (a, b) in enumerate(zip(pts, pts[1:]))]


def via(net, x, y, pid=900):
    return Piece(id=pid, net=net, conn=-1, kind="via", layer=-1, x1=x, y1=y, x2=x, y2=y, w=0.6, drill=0.3)


def ctx_of(b, cons, pieces=(), conns=(), status=None, stack=None, die=None):
    cfg = P.resolve(b, None)
    cfg.x.cons = cons
    cfg.x.cons_env = {"stack": stack or C.Stackup.default(b.L), "die": die or {}, "use_height": True}
    return Ctx(board=b, cfg=cfg, x=cfg.x, state=State(pieces, status), conns=list(conns), history={})


# ── KiCad custom rules ────────────────────────────────────────────────────
DRU = '''(version 1)
# a comment line (rule "ghost" (constraint skew (max 9mm)))
(rule "sd_match"
    (condition "A.NetName == '/SD_D?' || A.NetName == '/SD_CLK'")
    (constraint skew (max 0.5mm)))
(rule usb_skew (condition "A.inDiffPair('/USB_D_')") (constraint skew (max 0.15mm)))
(rule "usb_gap" (condition "A.inDiffPair('/USB_D')")
    (constraint diff_pair_gap (min 0.15mm) (opt 0.18mm) (max 0.9mm))
    (constraint diff_pair_uncoupled (max 6mm)))
(rule "xtal" (condition "A.NetName == '/XI' || A.NetName == '/XO'") (constraint length (max 8mm)))
(rule "ddr_len" (condition "A.NetClass == 'DDR'") (constraint length (min 30mm) (opt 31mm) (max 32mm)))
(rule "few_vias" (condition "A.hasNetclass('DDR') && A.Type == 'via'") (constraint via_count (max 2)))
(rule "rf_w" (layer "F.Cu") (condition "A.NetName == '/RF_OUT'") (constraint track_width (min 0.2mm) (opt 14mil)))
(rule "odd" (condition "A.NetName == '/SD_CLK' && B.NetName == '/SD_D0'") (constraint skew (max 0.1mm)))
(rule "timed" (condition "A.NetName == '/SD_*'") (constraint length (max 100ps)))
(rule "plain_min" (constraint clearance (min 0.127mm)))
(rule "tw_min" (condition "A.Type == 'track'") (constraint track_width (min 0.127mm)))
'''


def test_dru_subset_is_parsed_in_file_order():
    rules = C.parse_dru(DRU)
    assert [r.name for r in rules] == ["sd_match", "usb_skew", "usb_gap", "xtal", "ddr_len", "few_vias", "rf_w", "odd",
                                       "timed", "plain_min", "tw_min"]
    by = {r.name: r for r in rules}
    assert by["sd_match"].cons == (("skew", {"max": 0.5}, ()),)
    assert by["usb_gap"].cons == (("diff_pair_gap", {"min": 0.15, "opt": 0.18, "max": 0.9}, ()),
                                  ("diff_pair_uncoupled", {"max": 6.0}, ()))
    assert by["ddr_len"].cons[0][1] == {"min": 30.0, "opt": 31.0, "max": 32.0}
    assert by["rf_w"].layer == "F.Cu" and abs(by["rf_w"].cons[0][1]["opt"] - 14 * 0.0254) < 1e-12
    assert by["timed"].time_domain and by["timed"].cons[0][1] == {}                 # a delay rule: no mm value
    assert by["plain_min"].cond is None and by["few_vias"].cons[0][1] == {"max": 2.0}
    assert C.parse_dru("") == [] and C.parse_dru("(version 1)") == [] and C.parse_dru("(rule") == []


def test_conditions_the_subset_and_what_is_outside_it():
    pc = C.parse_condition
    assert pc(None) == [("all", "*")] and pc("  ") == [("all", "*")]
    assert pc("A.NetName == '/SD_D?' || A.NetName == '/SD_CLK'") == [("name", "/SD_D?"), ("name", "/SD_CLK")]
    assert pc("A.NetClass == 'DDR'") == [("class", "DDR")] and pc("A.hasNetclass('DDR')") == [("class", "DDR")]
    assert pc("A.inDiffPair('/USB_D_')") == [("pair", "/USB_D_")]
    assert pc("(A.NetName == '/A') || (A.inDiffPair('*') && A.Type == 'track')") == [("name", "/A"), ("pair", "*")]
    assert pc("A.Type == 'track' || A.Type == 'arc'") == [("all", "*"), ("all", "*")]
    # outside the subset: None (the rule is then listed as not read; KiCad still checks it)
    for cond in ("A.NetName == '/A' && B.NetName == '/B'", "A.NetName != '/A'", "A.Net != B.Net",
                 "A.NetName == '/A' && A.NetClass == 'X'", "A.intersectsArea('zone')", "A.Layer == 'F.Cu'",
                 "A.Pad_Type == 'SMD'", "AB.isCoupledDiffPair()"):
        assert pc(cond) is None, cond
    classes = {"/USB_D_P": "USB", "/USB_D_N": "USB", "/USB_DP": "USB", "/USB_DM": "USB", "/SD_D0": "Default",
               "/SD_CLK": "Default", "/A_P": "Default", "/CLK_P1": "HS,Default", "/CLK_N1": "HS,Default"}
    sel = C.select
    assert sel([("name", "/sd_*")], classes) == {"/SD_D0", "/SD_CLK"}                # KiCad compares case-insensitively
    assert sel([("name", "/SD_D?")], classes) == {"/SD_D0"}
    assert sel([("class", "USB")], classes) == {"/USB_D_P", "/USB_D_N", "/USB_DP", "/USB_DM"}
    assert sel([("class", "HS")], classes) == {"/CLK_P1", "/CLK_N1"}                 # one of several classes
    # inDiffPair: KiCad's suffix rule, the base with or without its trailing underscore, and the partner must exist
    assert sel([("pair", "/USB_D_")], classes) == {"/USB_D_P", "/USB_D_N"} == sel([("pair", "/USB_D")], classes)
    assert sel([("pair", "*")], classes) == {"/USB_D_P", "/USB_D_N", "/CLK_P1", "/CLK_N1"}   # /A_P alone, /USB_DP + _DM: no
    assert sel([("pair", "/CLK")], classes) == {"/CLK_P1", "/CLK_N1"} and sel([("pair", "/USB")], classes) == set()
    # trailing digits and underscores are skipped, then the last character decides (KiCad's MatchDpSuffix)
    assert C.dp_base("/CLK_P1") == ("/CLK_", 1) and C.dp_base("/D-") == ("/D", -1) and C.dp_base("/USB_DM") == (None, 0)
    assert C.dp_base("/CLK1_P") == ("/CLK1_", 1) and C.dp_base("/TXN_2") == ("/TX", -1)


RULE_BOARD_NETS = ["/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3", "/SD_CLK", "/USB_D_P", "/USB_D_N", "/XI", "/XO",
                   "/M_A0", "/M_A1", "/RF_OUT", "/LED"]


def rule_board():
    cls = {"/M_A0": {"class": "DDR"}, "/M_A1": {"class": "DDR"},
           "/USB_D_P": {"class": "USB", "dp_gap": 0.15, "dp_width": 0.22}, "/USB_D_N": {"class": "USB", "dp_gap": 0.15,
                                                                                    "dp_width": 0.22}}
    return board(two_parts(RULE_BOARD_NETS), cls)


def test_rules_become_groups_pairs_short_widths_and_unread_is_never_dropped():
    b = rule_board()
    cons, _cfg = resolve(b, None, DRU, {"Default": {"dp_gap": 0.2, "dp_width": 0.2, "dp_via_gap": 0.25},
                                        "USB": {"dp_gap": 0.15, "dp_width": 0.22, "dp_via_gap": 0.3}})
    assert cons.mode == "auto" and cons.any()
    g = {x.name: x for x in cons.groups}
    assert list(g) == ["sd_match", "ddr_len"], list(g)                    # file order; no name group on top of a rule's
    sd = g["sd_match"]
    assert sd.nets == ("/SD_CLK", "/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3") and sd.tol == 0.5 and sd.target is None
    assert (sd.mode, sd.src, sd.conf) == ("tune", "dru", 1.0) and "sd_match" in sd.why and "0.5" in sd.why
    ddr = g["ddr_len"]
    assert ddr.nets == ("/M_A0", "/M_A1") and (ddr.lo, ddr.target, ddr.hi, ddr.tol) == (30.0, 31.0, 32.0, 0.0)
    assert cons.tuned() == (sd, ddr) and cons.group_of("/SD_D2") is sd and cons.group_of("/LED") is None
    assert len(cons.pairs) == 1
    p = cons.pairs[0]
    assert (p.p, p.n, p.src, p.conf, p.kicad, p.coupled) == ("/USB_D_P", "/USB_D_N", "dru", 1.0, True, True)
    assert (p.skew, p.uncoupled, p.gap_lo, p.gap_hi) == (0.15, 6.0, 0.15, 0.9)
    assert p.gap == 0.18 and p.width == 0.22 and p.via_gap == 0.3         # the rule's opt gap, the class width / via gap
    assert cons.pair_of("/USB_D_N") is p and cons.coupled_pairs() == (p,)
    # length (max) alone = a keep-short net with a hard maximum
    assert set(cons.short) == {"/XI", "/XO", "/RF_OUT"} and cons.short["/XI"].max_len == 8.0 and cons.short["/XI"].hard
    assert cons.short["/RF_OUT"].src == "name" and not cons.short["/RF_OUT"].hard        # an RF feed by its name
    assert cons.short["/XI"].src == "dru" and "xtal" in cons.short["/XI"].why
    assert cons.max_vias == {"/M_A0": 2, "/M_A1": 2}
    w = cons.widths["/RF_OUT"]
    assert w.by_layer == ((0, 14 * 0.0254),) and w.src == "dru" and not w.neck
    assert abs(cons.width_on("/RF_OUT", 0) - 0.3556) < 1e-9 and cons.width_on("/RF_OUT", 1) == 0.2
    assert cons.width_on("/LED", 0) is None
    # a rule of ours with a condition outside the subset, and a time-domain rule: listed, never silently dropped
    assert cons.unread == ("odd", "timed") and any("time-domain" in n for n in cons.notes)
    # rules without a constraint of ours are not "unread" (a track_width minimum is the board minimum, not a class)
    pv = C.preview(cons)
    assert pv["unread"] == ["odd", "timed"] and pv["groups"][0]["src"] == "dru" and pv["pairs"][0]["skew"] == 0.15
    assert {"net": "/XI", "max": 8.0, "src": "dru", "why": cons.short["/XI"].why} in pv["short"]
    assert C.expected_set(cons) == {"groups": [["/M_A0", "/M_A1"], ["/SD_CLK", "/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3"]],
                                    "pairs": [["/USB_D_P", "/USB_D_N"]], "short": ["/RF_OUT", "/XI", "/XO"]}


def test_project_severity_ignore_and_rule_severity_are_listed_not_enforced():
    b = rule_board()
    cfg = P.resolve(b, None)
    proj = C.ProjectRules(rules=C.parse_dru(
        '(rule a (severity ignore) (condition "A.NetName == \'/SD_*\'") (constraint skew (max 0.5mm)))\n'
        '(rule b (condition "A.NetName == \'/M_A?\'") (constraint length (min 3mm) (max 9mm)))'),
        severities={"length_out_of_range": "ignore"})
    cons = C.resolve(b, cfg, cfg.x, proj)
    assert not [g for g in cons.groups if g.src == "dru"]
    assert sum("ignore" in n for n in cons.notes) == 2 and cons.unread == ()


def test_precedence_plan_over_rule_over_class_over_name():
    b = rule_board()
    # a plan pair keeps its own numbers; a field it leaves empty is filled from the rule
    cons, _ = resolve(b, {"diff_pairs": [{"p": "/USB_D_P", "n": "/USB_D_N", "skew": 0.4, "gap": 0.3}]}, DRU)
    p = cons.pairs[0]
    assert (p.src, p.skew, p.gap, p.uncoupled, p.gap_hi) == ("plan", 0.4, 0.3, 6.0, 0.9)
    # a plan group takes its nets; the rule's group keeps the rest (a net is in one group)
    cons, _ = resolve(b, {"groups": [{"nets": ["/SD_D0", "/SD_D1"], "tol": 0.2, "len": 40, "name": "mine"}]}, DRU)
    g = {x.name: x for x in cons.groups}
    assert g["mine"].nets == ("/SD_D0", "/SD_D1") and (g["mine"].src, g["mine"].tol, g["mine"].target) == ("plan", 0.2, 40.0)
    assert g["sd_match"].nets == ("/SD_CLK", "/SD_D2", "/SD_D3")
    # a rule group beats the name group for the same nets; without the rule the names give a REPORT-mode group
    cons, _ = resolve(b, None, "")
    sd = [x for x in cons.groups if "/SD_D0" in x.nets]
    assert len(sd) == 1 and (sd[0].mode, sd[0].src) == ("report", "name") and sd[0].conf < 1 and "typical" in sd[0].why
    assert sd[0].nets == ("/SD_CLK", "/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3") and cons.tuned() == ()
    assert cons.pairs[0].src == "class" and cons.pairs[0].skew is None       # class USB differs from Default's pair
    # vetoes: groups: [] = none at all; off = these nets; diff_pairs: [] = no pairs
    cons, _ = resolve(b, {"groups": []}, DRU)
    assert cons.groups == ()
    cons, _ = resolve(b, {"groups": [{"nets": ["/SD_*"], "off": True}]}, DRU)
    assert [x.name for x in cons.groups] == ["ddr_len"]
    cons, _ = resolve(b, {"diff_pairs": []}, DRU)
    assert cons.pairs == ()
    # modes: plan = only what the plan declares gets copper; report = everything measured, nothing tuned / coupled
    cons, _ = resolve(b, {"global": {"constraints": "plan"}, "groups": [{"nets": ["/M_A?"], "tol": 1}]}, DRU)
    assert [(x.name, x.mode, x.src) for x in cons.groups] == [("G1", "tune", "plan"), ("sd_match", "report", "dru")]
    assert [x.name for x in cons.tuned()] == ["G1"] and cons.coupled_pairs() == () and cons.widths == {} and cons.max_vias == {}
    cons, _ = resolve(b, {"global": {"constraints": "report"}}, DRU)
    assert cons.groups and cons.pairs and cons.tuned() == () and cons.coupled_pairs() == ()
    cons, _ = resolve(b, {"global": {"constraints": "off"}}, DRU)
    assert not cons.any() and cons.mode == "off" and C.preview(cons) == {}
    # the suggested plan sent back unchanged is still a guess from the names, not a declaration
    b2 = board(two_parts(["/USB_DP", "/USB_DM", "/XTAL1", "/XTAL2", "/A"]))
    sug = P.default_plan(b2)
    assert sug["diff_pairs"] == [{"p": "/USB_DP", "n": "/USB_DM", "gap": 0.2, "width": 0.2}]
    cons, _ = resolve(b2, sug)
    assert cons.pairs[0].src == "name" and cons.short["/XTAL1"].src == "name"
    sug["diff_pairs"][0]["skew"] = 0.2
    cons, _ = resolve(b2, sug)
    assert (cons.pairs[0].src, cons.pairs[0].skew, cons.pairs[0].conf) == ("plan", 0.2, 1.0)
    # plan widths / impedance labels; max_length makes a keep-short net hard
    cons, _ = resolve(b, {"nets": {"/LED": {"widths": {"B.Cu": 0.3}, "z": 50, "max_length": 40}, "/RF_OUT": {"z": 50}}}, DRU)
    assert cons.widths["/LED"].by_layer == ((1, 0.3),) and cons.widths["/LED"].z == 50.0 and cons.widths["/LED"].src == "plan"
    assert cons.width_on("/LED", 1) == 0.3 and cons.width_on("/LED", 0) == 0.2
    assert cons.widths["/RF_OUT"].src == "plan" and cons.widths["/RF_OUT"].by_layer == ()      # the plan wins over rf_w
    assert cons.short["/LED"].hard and cons.short["/LED"].max_len == 40.0 and cons.short["/LED"].src == "plan"


def test_a_skew_rule_and_a_length_rule_on_the_same_nets_are_one_group_with_both_checks():
    b = rule_board()
    cons, _ = resolve(b, None, '(rule m (condition "A.NetName == \'/SD_*\'") (constraint skew (max 0.5mm)))\n'
                               '(rule w (condition "A.NetName == \'/SD_*\'") (constraint length (min 20mm) (max 45mm)))')
    assert len(cons.groups) == 1
    g = cons.groups[0]
    assert (g.name, g.tol, g.lo, g.hi) == ("m", 0.5, 20.0, 45.0)
    lo, hi, ref = C.window(g, {"/SD_D0": 40.0, "/SD_D1": 46.0})
    assert (lo, hi, ref) == (45.5, 45.0, 46.0)                              # nothing fits: 46 breaks the rule's max
    lo, hi, ref = C.window(g, {"/SD_D0": 40.0, "/SD_D1": 41.0})
    assert (lo, hi, ref) == (40.5, 41.0, 41.0)
    # a pure window group: tol 0, only the rule's min / max count
    w = C.LenGroup(name="w", nets=("/a",), tol=0.0, target=31.0, lo=30.0, hi=32.0)
    assert C.window(w, {"/a": 5.0}) == (30.0, 32.0, 31.0)
    # an explicit target: len +- tol
    t = C.LenGroup(name="t", nets=("/a", "/b"), tol=0.5, target=42.0)
    assert C.window(t, {"/a": 10.0}) == (41.5, 42.5, 42.0)


# ── names: what is detected, and what must not be ─────────────────────────
def names_of(nets):
    return [sorted(g["nets"]) for g in C.name_groups(nets)]


def test_name_groups_detect_the_usual_interfaces():
    sd = ["/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3", "/SD_CLK", "/SD_CMD"]
    g = C.name_groups(sd + ["/LED", "/BTN"])
    assert len(g) == 1 and g[0]["kind"] == "sd" and g[0]["nets"] == sorted(sd) and g[0]["conf"] < 1
    assert names_of(["/emmc/DAT%d" % i for i in range(8)] + ["/emmc/CLK", "/emmc/CMD"]) == [
        sorted(["/emmc/DAT%d" % i for i in range(8)] + ["/emmc/CLK", "/emmc/CMD"])]
    q = ["/Q_IO0", "/Q_IO1", "/Q_IO2", "/Q_IO3", "/Q_CLK"]
    g = C.name_groups(q + ["/Q_CS"])
    assert [x["kind"] for x in g] == ["qspi"] and g[0]["nets"] == sorted(q)          # chip select is not length-matched
    tx = ["/RGMII_TXD%d" % i for i in range(4)] + ["/RGMII_TX_CTL", "/RGMII_TX_CLK"]
    rx = ["/RGMII_RXD%d" % i for i in range(4)] + ["/RGMII_RX_CTL", "/RGMII_RX_CLK"]
    g = C.name_groups(tx + rx)
    assert sorted(x["kind"] for x in g) == ["rgmii_rx", "rgmii_tx"] and sorted(sorted(x["nets"]) for x in g) == [sorted(rx), sorted(tx)]
    ddr = ["/DDR_DQ%d" % i for i in range(16)] + ["/DDR_DQS0_P", "/DDR_DQS0_N", "/DDR_DQS1_P", "/DDR_DQS1_N", "/DDR_DM0", "/DDR_DM1"]
    g = C.name_groups(ddr)
    assert [x["kind"] for x in g] == ["ddr_lane", "ddr_lane"]
    assert sorted(g[0]["nets"]) == sorted(["/DDR_DQ%d" % i for i in range(8)] + ["/DDR_DQS0_P", "/DDR_DQS0_N", "/DDR_DM0"])
    lcd = ["/LCD_R%d" % i for i in range(5)] + ["/LCD_PCLK", "/LCD_HSYNC", "/LCD_VSYNC", "/LCD_DE"]
    assert names_of(lcd) == [sorted(lcd)]
    gen = ["/ADC_D%d" % i for i in range(6)] + ["/ADC_CLK"]
    g = C.name_groups(gen)
    assert [x["kind"] for x in g] == ["bus"] and g[0]["conf"] == C.BUS_CONF and g[0]["nets"] == sorted(gen)
    assert C.name_groups(sd) == C.name_groups(list(reversed(sd)))                    # order of the input does not matter


def test_name_groups_false_positives_are_not_detected():
    none = [
        ["/B%02d" % i for i in range(24)],                                  # synth_bus_cross: a bus without a clock
        ["/D%d" % i for i in range(8)],                                     # numbered lines, no clock
        ["/D%d" % i for i in range(8)] + ["/CLK"],                          # ... + a clock, but no common stem
        ["/GPIO%d" % i for i in range(1, 9)] + ["/I2C_SCL", "/I2C_SDA"],
        ["/LED_1", "/LED_2", "/LED_3", "/LED_4", "/LED_CLK"],               # numbers without a letter are not a bus
        ["/A_D0", "/A_D1", "/A_D2", "/B_CLK", "/B_D3"],                     # the clock is on another stem
        ["/X_D0", "/X_D1", "/X_D2", "/X_CLK"],                              # three members: below the generic minimum
        ["/M_D0", "/M_D1", "/M_D2", "/M_D3", "/M_CLK", "/M_SCK"],           # two clocks: which one?
        ["/SD_D0", "/SD_D1", "/SD_CLK"],                                    # SD needs four data lines
        ["/UART_TXD0", "/UART_TX_CLK"],                                     # one TXD is no RGMII
        ["/SPI_MOSI", "/SPI_MISO", "/SPI_SCK", "/SPI_CS"],                  # plain SPI: no numbered members
        ["/A0", "/A1", "/A2", "/A3", "/A4", "/A5", "/A6", "/A7", "/CK"],    # address-like names without a DDR stem
        ["/DDR_DQ0", "/DDR_DQ1", "/DDR_DQ2", "/DDR_DQ3"],                   # DQ lines without a strobe
        ["/XTAL1", "/XTAL2", "/RESET", "/BOOT0"],
    ]
    for nets in none:
        assert C.name_groups(nets) == [], (nets, C.name_groups(nets))


def test_name_pairs_confidence_kicad_rule_and_supply_names():
    pads = two_parts(["/USB_D_P", "/USB_D_N", "/A", "/B", "/CAN_H", "/C", "/D", "/E", "/CAN_L"])
    pads += [("J9", 1, "/LVDS_P", 50, 10), ("U9", 1, "/LVDS_P", 50, 30), ("J8", 1, "/LVDS_N", 55, 10), ("U8", 1, "/LVDS_N", 55, 30)]
    pads += two_parts(["/VDD_P", "/VDD_N", "/SDA", "/SCL", "/RX", "/TX", "/STEP", "/STEN"], x1=10, x2=40, y0=30)
    b = board(pads)
    cons, _ = resolve(b, None)
    by = {p.p: p for p in cons.pairs}
    assert set(by) == {"/USB_D_P", "/CAN_H", "/LVDS_P"}, set(by)        # no supply pair, no SDA/SCL, RX/TX, STEP/STEN
    usb, can, lvds = by["/USB_D_P"], by["/CAN_H"], by["/LVDS_P"]
    assert (usb.n, usb.src, usb.conf, usb.coupled, usb.kicad) == ("/USB_D_N", "name", 0.9, True, True)
    assert "adjacent" in usb.why
    assert (can.n, can.conf, can.coupled, can.kicad) == ("/CAN_L", 0.6, True, False)      # same parts, pads apart
    assert (lvds.n, lvds.conf, lvds.coupled) == ("/LVDS_N", 0.3, False)                   # different parts: two nets
    assert any("/CAN_H + /CAN_L is not a pair by KiCad" in n for n in cons.notes)
    assert usb.skew is None and usb.uncoupled is None and usb.gap == 0.2 and usb.width == 0.2
    assert C.pair_confidence(b, "/USB_D_P", "/USB_D_N")[0] == 0.9
    # the same names with the pads far apart on the same parts
    b2 = board(two_parts(["/USB_D_P", "/X1", "/X2", "/X3", "/X4", "/USB_D_N"]))
    assert C.pair_confidence(b2, "/USB_D_P", "/USB_D_N")[0] == 0.6
    # impedance classes: the name pair is labelled, single-ended nets of such a class get a width rule
    b3 = board(two_parts(["/ETH_P", "/ETH_N", "/CLK50", "/GND1"]),
               {"/ETH_P": {"class": "100ohm", "dp_gap": 0.25, "dp_width": 0.16},
                "/ETH_N": {"class": "100ohm", "dp_gap": 0.25, "dp_width": 0.16}, "/CLK50": {"class": "50R", "width": 0.16}})
    cons, _ = resolve(b3, None)
    assert (cons.pairs[0].src, cons.pairs[0].z, cons.pairs[0].width, cons.pairs[0].gap) == ("class", 100.0, 0.16, 0.25)
    assert cons.pairs[0].conf == 1.0 and set(cons.widths) == {"/CLK50"} and cons.widths["/CLK50"].z == 50.0
    assert [C._impedance(s) for s in ("90ohm", "100R", "Z50", "USB_90Ohm", "50R0", "DDR3", "Default", "pwr", "R100", "48V")] == [
        90.0, 100.0, 50.0, 90.0, 50.0, None, None, None, None, None]


def test_keep_short_names_crystals_switch_nodes_rf_feeds():
    pads = two_parts(["/XTAL1", "/XTAL2", "/SW", "/ANT", "/SWDIO", "/RFID_EN", "/PHY_RST", "/LX1"])
    pads += [("L1", 1, "/SW", 20, 20), ("L2", 1, "/LX1", 22, 20)]
    b = board(pads)
    cons, _ = resolve(b, None)
    assert set(cons.short) == {"/XTAL1", "/XTAL2", "/SW", "/ANT", "/LX1"}, set(cons.short)   # not SWDIO / RFID_EN / PHY_RST
    assert all(not s.hard and s.max_len is None and s.src == "name" for s in cons.short.values())
    assert "switch node" in cons.short["/SW"].why and "antenna" in cons.short["/ANT"].why
    # the same switch-node name without an inductor on the net is not one
    b2 = board(two_parts(["/SW", "/A"]))
    assert "/SW" not in resolve(b2, None)[0].short


def test_boards_without_constraints_resolve_to_nothing():
    b = board(two_parts(["/B%02d" % i for i in range(24)], reverse=True))            # the shape of synth_bus_cross
    cons, _ = resolve(b, None)
    assert not cons.any() and cons.groups == () and cons.pairs == () and C.preview(cons) == {}
    assert C.measure(ctx_of(b, cons)) == {}
    assert C.measure(ctx_of(b, None)) == {}


def test_corpus_boards_detect_what_the_design_says():
    """synth_bus_cross: nothing. phone_breathalyzer: one name pair KiCad does not see as one, and its rule file
    holds no constraint of ours. cm5_minima: its _P/_N pairs with their class dimensions. (KiCad's Python reads
    the boards; skipped when the corpus is not on this machine.)"""
    if not (CORPUS / "synth_bus_cross" / "synth_bus_cross.kicad_pcb").exists():
        print("      (bench/route/corpus not present: skipped)")
        return
    from copper_kicad import project as projmod
    from copper_kicad.pcb.guided import session

    def load(name):
        pr = projmod.Project(CORPUS / name, name)
        b = session.load_board(pr)[0]
        cfg = P.resolve(b, None)
        proj = C.read_project(pr, layers=b.L)
        return b, proj, C.resolve(b, cfg, cfg.x, proj)

    b, proj, cons = load("synth_bus_cross")
    assert cons.groups == () and cons.pairs == () and not cons.any() and proj.stack.n == 2
    assert abs(proj.stack.height(0, 1) - 1.58) < 1e-9
    b, proj, cons = load("phone_breathalyzer")
    assert len(proj.rules) == 13 and cons.unread == ()                   # no constraint of ours, nothing unread
    assert not [g for g in cons.groups if g.src == "dru"] and cons.max_vias == {} and not cons.tuned()
    assert [(p.p, p.n, p.kicad, p.src) for p in cons.pairs] == [("/USB_DP", "/USB_DM", False, "name")]
    if (CORPUS / "cm5_minima" / "cm5_minima.kicad_pcb").exists():
        b, proj, cons = load("cm5_minima")
        assert proj.stack.explicit and proj.stack.n == b.L == 6 and proj.die and proj.use_height
        assert len(cons.pairs) >= 10 and all(p.kicad for p in cons.pairs if p.p.endswith("_P"))
        z = {p.z for p in cons.pairs}
        assert 90.0 in z and 100.0 in z
        for p in cons.pairs:
            net = b.nets[p.p]
            assert abs(p.gap - net.dp_gap) < 1e-9 and abs(p.width - net.dp_width) < 1e-9, p
            if p.z == 90.0:
                assert (p.width, p.gap, p.src) == (0.14, 0.154, "class"), p
        assert not cons.tuned()                                          # name / class constraints never get copper alone


def test_bench_rule_files_parse_and_match_their_meta():
    """The four constraint boards of the benchmark: every rule is read (nothing unread) — skipped until
    bench/route/prepare.py has built them."""
    import json
    seen = 0
    for d in sorted(CORPUS.glob("cons_*")):
        dru, meta = d / f"{d.name}.kicad_dru", d / "meta.json"
        if not dru.exists() or not meta.exists():
            continue
        seen += 1
        rules = C.parse_dru(dru.read_text(encoding="utf-8"))
        assert rules, d.name
        for r in rules:
            mine = [c for c in r.cons if c[0] in C.OURS]
            assert mine and C.parse_condition(r.cond) is not None and not r.time_domain, (d.name, r.name)
        assert json.loads(meta.read_text(encoding="utf-8")).get("suite") == "cons"
    if not seen:
        print("      (no bench/route/corpus/cons_* boards: skipped)")


# ── stackup, pad-to-die ───────────────────────────────────────────────────
def test_stackup_default_and_from_the_board_file():
    s4, s2 = C.Stackup.default(4), C.Stackup.default(2)
    assert [round(s4.height(0, k), 4) for k in (1, 2, 3)] == [0.5325, 1.0475, 1.58]        # measured, KiCad 10.0
    assert round(s4.height(1, 2), 4) == 0.515 and round(s2.height(0, 1), 4) == 1.58        # inner to inner: measured too
    assert s4.height(2, 0) == s4.height(0, 2) and s4.height(1, 1) == 0.0
    txt = '''(kicad_pcb (version 20241229) (general (thickness 1.2) (legacy_teardrops no))
      (setup (stackup
        (layer "F.SilkS" (type "Top Silk Screen")) (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
        (layer "F.Cu" (type "copper") (thickness 0.035))
        (layer "dielectric 1" (type "prepreg") (thickness 0.1) (material "2116") (epsilon_r 4.45) addsublayer (thickness 0.05))
        (layer "In1.Cu" (type "copper") (thickness 0.0152))
        (layer "dielectric 2" (type "core") (thickness 0.5) (epsilon_r 4.6))
        (layer "In2.Cu" (type "copper") (thickness 0.0152))
        (layer "dielectric 3" (type "prepreg") (thickness 0.0994))
        (layer "B.Cu" (type "copper") (thickness 0.035))
        (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01)) (copper_finish "None")))
      (footprint "x" (pad "1" smd rect (at 1 2) (net "/A B") (pintype "input") (die_length 0.84) (uuid "u"))
                     (pad "2" smd rect (at 1 3) (net 7 "/A B") (die_length 0.16))
                     (pad "3" smd rect (at 1 4) (net "/C"))
                     (pad "4" smd rect (at 1 5) (net "/D") (die_length 0)))'''
    st = C.Stackup.from_text(txt, 4)
    assert st.explicit and st.n == 4
    assert abs(st.height(0, 1) - (0.035 + 0.15 + 0.0076)) < 1e-9 and st.items[1] == ("di", 0.15000000000000002, 4.45) or \
        abs(st.items[1][1] - 0.15) < 1e-12
    assert abs(st.height(1, 2) - (0.0076 + 0.5 + 0.0076)) < 1e-9
    assert abs(st.height(0, 3) - (0.035 + 0.15 + 0.0152 + 0.5 + 0.0152 + 0.0994 + 0.035)) < 1e-9
    # a stackup that does not list the board's copper layers, or no stackup: KiCad's default for the board thickness
    d2 = C.Stackup.from_text(txt, 2)
    assert not d2.explicit and abs(d2.height(0, 1) - (1.2 - 0.02)) < 1e-9
    assert abs(C.Stackup.from_text("(kicad_pcb (general (thickness 1.6)))", 2).height(0, 1) - 1.58) < 1e-9
    assert C.die_lengths(txt) == {"/A B": 1.0}


# ── length, as KiCad's DRC counts it (figures: KiCad 10.0, design §14.9) ──
PAD_Y = {"/a": 3.0, "/b": 6.0, "/c": 9.0, "/d": 12.0, "/e": 15.0, "/h": 24.0, "/i": 27.0}
PADENTRY = {
    "/a": ([(5, 3), (35, 3)], 30.0000),                                          # centre to centre
    "/b": ([(4.5, 6), (35.5, 6)], 31.0000),                                      # through both centres: in full
    "/c": ([(5, 9), (5.2, 9.2), (34.8, 9.2), (35, 9)], 30.0649),                 # dogleg inside the pad: cut short
    "/d": ([(5.6, 12), (34.4, 12)], 28.8000),                                    # ends on the pad edges
    "/e": ([(5.3, 15), (34.7, 15)], 29.4000),                                    # ends inside, not on the centre
    "/h": ([(5, 24), (5, 24.6), (35, 24.6), (35, 24)], 31.2000),                 # straight out through the long side
    "/i": ([(5, 27), (5.25, 27.25), (34.75, 27.25), (35, 27)], 30.1000),         # 45 degrees, leaves through the end
}


def length_of(b, net, pieces, stack=None, die=None):
    cons = C.Constraints()
    return C.net_length(ctx_of(b, cons, pieces, stack=stack, die=die), net)


def test_pad_entry_shortcut_matches_kicad():
    b = board([(ref, i + 1, n, x, PAD_Y[n]) for i, n in enumerate(sorted(PAD_Y)) for ref, x in (("J1", 5.0), ("U1", 35.0))])
    for net, (pts, want) in PADENTRY.items():
        lp = length_of(b, net, seg(net, pts))
        assert abs(lp.total - want) < 6e-5, (net, lp, want)
        assert not lp.branched and lp.vias == 0 and lp.via == 0.0
    c = length_of(b, "/c", seg("/c", PADENTRY["/c"][0]))
    assert abs(c.track - 30.16568) < 1e-4 and abs(c.inpad - 2 * (0.2 * math.sqrt(2) + 0.4 - math.hypot(0.6, 0.2))) < 1e-6
    # the copper that was on the board before the run counts the same way (board.kept_tracks)
    pts = PADENTRY["/i"][0]
    kept = [["/i", 0, a[0], a[1], c_[0], c_[1], 0.2, 0, 0] for a, c_ in zip(pts, pts[1:])]
    bk = board([(ref, i + 1, n, x, PAD_Y[n]) for i, n in enumerate(sorted(PAD_Y)) for ref, x in (("J1", 5.0), ("U1", 35.0))],
               tracks=kept)
    assert abs(length_of(bk, "/i", []).total - 30.1000) < 6e-5


def test_two_chains_ending_on_one_pad_centre_are_counted_in_full():
    """Measured (engineer-A probe): J1 -> R1 -> U1 with a dogleg inside R1's pad on both sides = 30.3234 mm, the
    plain sum: for KiCad the two tracks are one line through the pad, so nothing is cut short there."""
    b = board([("J1", 1, "/T", 5, 20), ("U1", 1, "/T", 35, 20), ("R1", 1, "/T", 20, 22)])
    c1 = [(5, 20), (18, 21.9), (19.8, 21.9), (20, 22)]
    c2 = [(20, 22), (20.2, 21.9), (22, 21.9), (35, 20)]
    lp = length_of(b, "/T", seg("/T", c1) + seg("/T", c2, first=50))
    assert abs(lp.total - 30.3234) < 6e-5 and lp.inpad < 1e-6 and lp.branched           # three pads: flagged


def test_via_hops_pad_to_die_and_tee_match_kicad():
    nets = {"/A": 4.0, "/B": 8.0, "/C": 12.0, "/E": 16.0, "/T": 20.0, "/V": 26.0, "/W": 30.0}
    pads = [(ref, i + 1, n, x, y) for i, (n, y) in enumerate(nets.items()) for ref, x in (("J1", 5.0), ("U1", 35.0))]
    pads.append(("R1", 1, "/T", 20.0, 22.0))
    b = board(pads, layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    want = {"/A": (1, 31.0650), "/B": (2, 32.0950), "/C": (3, 33.1600)}
    for net, (lay, total) in want.items():
        y = nets[net]
        pcs = seg(net, [(5, y), (15, y)]) + seg(net, [(15, y), (25, y)], layer=lay, first=10) + seg(net, [(25, y), (35, y)], first=20)
        pcs += [via(net, 15, y, 901), via(net, 25, y, 902)]
        lp = length_of(b, net, pcs)
        assert abs(lp.total - total) < 6e-5 and lp.vias == 2 and abs(lp.track - 30.0) < 1e-9, (net, lp)
    e = length_of(b, "/E", seg("/E", [(5, 16), (35, 16)]), die={"/E": 2.5})
    assert abs(e.total - 32.5) < 1e-9 and e.die == 2.5
    t = length_of(b, "/T", seg("/T", [(5, 20), (20, 20), (35, 20)]) + seg("/T", [(20, 20), (20, 22)], first=10))
    assert abs(t.total - 32.0) < 1e-9 and t.branched                                    # the tee stub counts
    # a via in the middle of a straight F.Cu track with a B.Cu branch: F.Cu still counts as used at the via (34.58)
    v = length_of(b, "/V", seg("/V", [(5, 26), (20, 26), (35, 26)]) + seg("/V", [(20, 26), (20, 29)], layer=3, first=10)
                  + [via("/V", 20, 26)])
    assert abs(v.total - 34.58) < 6e-5
    # F.Cu -> In1.Cu -> In2.Cu -> F.Cu: 30 + 0.5325 + 0.515 + 1.0475 = 32.095
    w = length_of(b, "/W", seg("/W", [(5, 30), (12, 30)]) + seg("/W", [(12, 30), (20, 30)], layer=1, first=10)
                  + seg("/W", [(20, 30), (28, 30)], layer=2, first=20) + seg("/W", [(28, 30), (35, 30)], first=30)
                  + [via("/W", 12, 30, 901), via("/W", 20, 30, 902), via("/W", 28, 30, 903)])
    assert abs(w.total - 32.0950) < 6e-5 and w.vias == 3
    # a via nobody leaves on another layer adds nothing; use_height_for_length_calcs = false: vias add nothing at all
    idle = length_of(b, "/A", seg("/A", [(5, 4), (35, 4)]) + [via("/A", 20, 4)])
    assert abs(idle.total - 30.0) < 1e-9 and idle.vias == 1
    cx = ctx_of(b, C.Constraints(), seg("/C", [(5, 12), (15, 12)]) + seg("/C", [(15, 12), (35, 12)], layer=3, first=10)
                + [via("/C", 15, 12), via("/C", 35, 12, 901)])
    assert abs(C.net_length(cx, "/C").total - (30.0 + 2 * 1.58)) < 6e-5                 # the via in the SMD pad counts
    cx.x.cons_env["use_height"] = False
    assert abs(C.net_length(cx, "/C").total - 30.0) < 1e-9


def test_dogleg_inside_a_via_pad_is_cut_short_like_one_inside_a_pad():
    """Measured (engineer-A probe on the routed cons_mixed board, KiCad 10.0): a track that turns s mm before its
    via, inside the via's 0.6 mm pad, is counted as the straight line from where it enters the via's circle to the
    via centre: s + sqrt(r^2 - s^2) - r shorter. A stub as long as the radius (or longer) is counted in full."""
    b = board([("U1", 6, "/Q", 120.5, 121.2, 0.75, 0.225), ("U2", 2, "/Q", 106.3, 119.365, 0.75, 0.3)],
              layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    kicad = {0.01: 21.0585, 0.035: 21.0426, 0.05: 21.0342, 0.1: 21.0126, 0.2: 21.0055, 0.3: 21.1674, 0.5: 21.2605}
    for stub, want in kicad.items():
        y = 121.2 - stub
        pcs = seg("/Q", [(120.5, 121.2), (119.3, 121.2), (119.3, y)]) + [via("/Q", 119.3, y, 901)]
        pcs += seg("/Q", [(104.7, 120.165), (118.3, 120.165), (119.3, y)], layer=2, first=10)
        pcs += seg("/Q", [(106.3, 119.365), (105.1, 119.365), (104.3, 120.165), (104.7, 120.165)], first=20)
        pcs += [via("/Q", 104.7, 120.165, 902)]
        lp = length_of(b, "/Q", pcs)
        assert abs(lp.total - want) < 6e-5, (stub, lp, want)
        assert abs(lp.via - 2.095) < 1e-9 and lp.vias == 2
        cut = stub + math.sqrt(0.09 - stub * stub) - 0.3 if stub < 0.3 else 0.0
        assert abs(lp.inpad - cut) < 1e-6, (stub, lp.inpad, cut)


def test_u_bump_and_pair_with_a_layer_change_match_kicad():
    b = board(two_parts(["/USB_D_P", "/USB_D_N", "/SD_CLK", "/SD_D0"], y0=4.0))
    b = board([("J1", 1, "/USB_D_P", 5, 4), ("U1", 1, "/USB_D_P", 35, 4), ("J1", 2, "/USB_D_N", 5, 5),
               ("U1", 2, "/USB_D_N", 35, 5), ("J1", 3, "/SD_D0", 5, 16), ("U1", 3, "/SD_D0", 35, 16)])
    A, c, F, S = 1.0, 0.2, 0.6, 0.6
    pts, s = [(5, 16)], 10.0
    for _k in range(2):
        pts += [(s - c, 16), (s, 16 - c), (s, 16 - A + c), (s + c, 16 - A), (s + F - c, 16 - A), (s + F, 16 - A + c),
                (s + F, 16 - c), (s + F + c, 16)]
        s += F + S
    lp = length_of(b, "/SD_D0", seg("/SD_D0", pts + [(35, 16)]))
    assert abs(lp.total - 33.0627) < 6e-5                                               # 30 + 2 (2A - 4c (2 - sqrt 2))
    pcs = []
    for n, y0, yl, yv, k in (("/USB_D_P", 4.0, 4.3, 4.05, 0), ("/USB_D_N", 5.0, 4.7, 4.95, 100)):
        pcs += seg(n, [(5, y0), (6, y0), (6.3, yl), (18, yl), (18.25, yv), (19, yv)], first=k + 1)
        pcs += seg(n, [(19, yv), (19.75, yv), (20, yl), (33.7, yl), (34, y0), (35, y0)], layer=1, first=k + 20)
        pcs += [via(n, 19, yv, 900 + k), via(n, 35, y0, 901 + k)]
    for n in ("/USB_D_P", "/USB_D_N"):
        assert abs(length_of(b, n, pcs).total - 33.6156) < 6e-5, n


# ── pair coupling (geometry) ──────────────────────────────────────────────
def lanes(pp, nn, w=0.2, layer=0):
    return ([(layer, a[0], a[1], b[0], b[1], w) for a, b in zip(pp, pp[1:])],
            [(layer, a[0], a[1], b[0], b[1], w) for a, b in zip(nn, nn[1:])])


def test_coupled_stats():
    sp, sn = lanes([(5, 4), (6, 4), (6.3, 4.3), (33.7, 4.3), (34, 4), (35, 4)],
                   [(5, 5), (6, 5), (6.3, 4.7), (33.7, 4.7), (34, 5), (35, 5)])
    cs = C.coupled_stats(sp, sn, 0.2, 0.2)
    assert abs(cs["coupled_mm"][0] - 27.4) < 1e-9 and abs(cs["coupled_mm"][1] - 27.4) < 1e-9
    assert abs(cs["uncoupled_mm"][0] - (2 + 2 * 0.3 * math.sqrt(2))) < 1e-9 and cs["gap_seen"] == 0.2
    assert abs(cs["coupled_pct"] - 100 * 27.4 / (29.4 + 2 * 0.3 * math.sqrt(2))) < 1e-9
    # with a rule window 0.15 .. 0.9 the straight exits at pad pitch (edge gap 0.8) count as coupled too
    cs = C.coupled_stats(sp, sn, 0.2, 0.2, 0.15, 0.9)
    assert abs(cs["coupled_mm"][0] - 29.4) < 1e-9
    # today's router: N beside P at the clearance (0.2), the pair's gap is 0.15 -> nothing is at the gap
    cs = C.coupled_stats(sp, sn, 0.2, 0.15)
    assert cs["coupled_mm"] == [0.0, 0.0] and cs["coupled_pct"] == 0.0 and cs["gap_seen"] == 0.2
    # different layers, or not parallel: not coupled, and no gap to report
    cs = C.coupled_stats(sp, [(1,) + t[1:] for t in sn], 0.2, 0.2)
    assert cs["coupled_pct"] == 0.0 and cs["gap_seen"] is None
    # partial overlap: only the stretch that has a neighbour
    sp, sn = lanes([(0, 0), (10, 0)], [(4, 0.4), (20, 0.4)])
    cs = C.coupled_stats(sp, sn, 0.2, 0.2)
    assert abs(cs["coupled_mm"][0] - 6.0) < 1e-9 and abs(cs["coupled_mm"][1] - 6.0) < 1e-9
    assert abs(cs["coupled_pct"] - 37.5) < 1e-9                                        # of the longer net (16 mm)
    assert C.coupled_stats([], [], 0.2, 0.2)["coupled_pct"] == 0.0


def test_coupled_stats_is_the_same_for_copper_read_back_from_a_board_file():
    """Fix round 1: copper read back from a board file has its coordinates rounded to 0.1 um. The short pieces of
    a skew bump (0.4 mm) are then 2e-4 rad off their neighbour, and an exact parallel test dropped them: one
    and the same copper was 84 % coupled / 3.4 mm uncoupled in the run that laid it and 82 % / 3.8 mm in a
    later call that only measured it (KiCad says 3.4008 mm). The figures must not depend on that rounding."""
    s2 = math.sqrt(0.5)
    p0 = (103.325, 109.325)
    pp = [p0, (p0[0] + 20 * s2, p0[1] + 20 * s2)]
    # the N lane 0.35 mm beside it (gap 0.15), cut into pieces of 0.4 .. 0.7 mm as skew bumps leave them
    off = (0.35 * s2, -0.35 * s2)
    cuts = [0.0, 0.4, 1.1, 1.5, 2.2, 2.6, 9.0, 9.4, 20.0]
    nn = [(p0[0] + off[0] + t * s2, p0[1] + off[1] + t * s2) for t in cuts]
    exact = C.coupled_stats(*lanes(pp, nn), 0.2, 0.15)
    assert abs(exact["coupled_mm"][0] - 20.0) < 1e-9 and exact["gap_seen"] == 0.15
    r4 = lambda pts: [(round(x, 4), round(y, 4)) for x, y in pts]     # noqa: E731  (what the board extract hands out)
    back = C.coupled_stats(*lanes(r4(pp), r4(nn)), 0.2, 0.15)
    assert abs(back["coupled_mm"][0] - exact["coupled_mm"][0]) < 1e-3, (back, exact)
    assert abs(back["coupled_mm"][1] - exact["coupled_mm"][1]) < 1e-3
    assert abs(back["uncoupled_mm"][0] - exact["uncoupled_mm"][0]) < 1e-3
    assert round(back["coupled_pct"]) == round(exact["coupled_pct"]) == 100 and back["gap_seen"] == 0.15
    # ... and a piece that really runs at an angle is still not coupled: 1 degree over 5 mm, 5 degrees over 0.4 mm
    for ln, deg in ((5.0, 1.0), (0.4, 5.0)):
        a = math.radians(deg)
        sp, sn = lanes([(0, 0), (ln, 0)], [(0, 0.35), (ln * math.cos(a), 0.35 + ln * math.sin(a))])
        assert C.coupled_stats(sp, sn, 0.2, 0.15)["coupled_mm"] == [0.0, 0.0], (ln, deg)


def test_lanes_of_a_pair_that_was_on_the_board_before_the_run_come_from_its_copper():
    """Fix round 1: a call that routes nothing (every connection is already made) has no record of how a pair was
    laid, and said `lanes: false` about the very copper the routing call had reported as `lanes: true`. For
    copper from before the run `lanes` is what the copper is: side by side at its gap for half its length."""
    dru = '(rule usb (condition "A.inDiffPair(\'/USB_D_\')") (constraint skew (max 0.15mm)))'
    pads = [("J1", 1, "/USB_D_P", 5, 10), ("J1", 2, "/USB_D_N", 5, 11), ("U1", 1, "/USB_D_P", 35, 10),
            ("U1", 2, "/USB_D_N", 35, 11)]
    cl = {n: {"class": "DP", "dp_gap": 0.15, "dp_width": 0.2} for n in ("/USB_D_P", "/USB_D_N")}

    def kept(pp, nn):
        return [["/USB_D_P", 0, a[0], a[1], b[0], b[1], 0.2, 0, False] for a, b in zip(pp, pp[1:])] + \
               [["/USB_D_N", 0, a[0], a[1], b[0], b[1], 0.2, 0, False] for a, b in zip(nn, nn[1:])]
    side = kept([(5, 10), (6, 10), (6.325, 10.325), (33.675, 10.325), (34, 10), (35, 10)],
                [(5, 11), (6, 11), (6.325, 10.675), (33.675, 10.675), (34, 11), (35, 11)])
    b = board(pads, classes=cl, tracks=side)
    cons, _ = resolve(b, None, dru)
    assert cons.pairs[0].coupled
    row = C.measure(ctx_of(b, cons))["pairs"][0]                      # no connection of this run, no history
    assert row["lanes"] is True and row["coupled"] >= 90 and "why" not in row and row["ok"] is True
    # the same two nets a millimetre apart all the way: not lanes, and the row says what it is looking at
    apart = kept([(5, 10), (35, 10)], [(5, 11), (35, 11)])
    b2 = board(pads, classes=cl, tracks=apart)
    cons2, _ = resolve(b2, None, dru)
    row = C.measure(ctx_of(b2, cons2))["pairs"][0]
    assert row["lanes"] is False and row["coupled"] == 0 and "copper from before this run" in row["why"]


def test_a_per_layer_width_rule_gives_every_layer_its_own_width_and_the_base_width_elsewhere():
    """Fix round 1: with nets[..].widths {In2.Cu: 0.3, B.Cu: 0.25} the pad escapes on F.Cu were laid at 0.3 mm —
    the widest of the listed layers — where the unlisted layer keeps the net's own 0.2 mm. ``lay_width`` is the
    one answer every stage that lays full-width copper asks (fan-out, detail, the fallback search)."""
    b = board(two_parts(["/P0", "/P1", "/Q"]), layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    cons, cfg = resolve(b, {"nets": {"/P*": {"widths": {"In2.Cu": 0.3, "B.Cu": 0.25}}}})
    assert cons.layered("/P0") and cons.layered("/P1") and not cons.layered("/Q")
    full = 0.3                                                       # what the search runs at: the widest layer
    assert [cons.lay_width("/P0", l, full) for l in range(4)] == [0.2, 0.2, 0.3, 0.25]
    assert cons.lay_width("/Q", 0, 0.2) == 0.2 and cons.lay_width("/P0", 0, 0.5) == 0.5      # not this rule's width
    # a rule that lists the top layer wider than the rest: the unlisted layers keep the plan's `width`
    cons, _ = resolve(b, {"nets": {"/P0": {"width": 0.15, "widths": {"F.Cu": 0.4}}}})
    assert [cons.lay_width("/P0", l, 0.4) for l in range(4)] == [0.4, 0.15, 0.15, 0.15]
    # report mode lays nothing differently
    cons, _ = resolve(b, {"global": {"constraints": "report"}, "nets": {"/P0": {"widths": {"B.Cu": 0.25}}}})
    assert not cons.layered("/P0") and cons.lay_width("/P0", 3, 0.25) == 0.25
    # the fan-out stage asks it for the pad's own layer
    from copper_kicad.pcb.v2 import fanout
    cons, _ = resolve(b, {"nets": {"/P*": {"widths": {"In2.Cu": 0.3, "B.Cu": 0.25}}}})
    job = type("J", (), {"net": "/P0", "layer": 0, "width": 0.3, "neck": 0.3})()
    fanout._layer_width(type("X", (), {"x": type("V", (), {"cons": cons})()})(), job)
    assert (job.width, job.neck) == (0.2, 0.2)
    job = type("J", (), {"net": "/P0", "layer": 3, "width": 0.3, "neck": 0.3})()
    fanout._layer_width(type("X", (), {"x": type("V", (), {"cons": cons})()})(), job)
    assert (job.width, job.neck) == (0.25, 0.25)
    job = type("J", (), {"net": "/Q", "layer": 0, "width": 0.2, "neck": 0.15})()
    fanout._layer_width(type("X", (), {"x": type("V", (), {"cons": cons})()})(), job)
    assert (job.width, job.neck) == (0.2, 0.15)


# ── KiCad's constraint markers (messages verbatim from KiCad 10.0) ────────
def v(typ, desc, items, severity="error"):
    return {"type": typ, "severity": severity, "description": desc,
            "items": [{"description": i, "pos": {"x": 1, "y": 2}, "uuid": f"u{k}"} for k, i in enumerate(items)]}


RAW = [
    v("length_out_of_range", "Track length out of range (rule 'probe' min length 2000.0000 mm; actual 31.0650 mm)",
      ["Track [/A] on In1.Cu, length 10.0000 mm", "Track [/A] on F.Cu, length 10.0000 mm"]),
    v("length_out_of_range", "Track length out of range (rule 'minlen' min length 10.0000 mm; actual 0.0000 mm)",
      ["Pad 4 [/SD_D0] of U1 on F.Cu", "Pad 4 [/SD_D0] of J1 on F.Cu"]),
    v("skew_out_of_range", "Skew between tracks out of range (rule 'sd_match' max skew 0.3000 mm; actual -3.0627 mm; "
      "target net length 33.0627 mm (from /SD_D3); actual 30.0000 mm)", ["Pad 3 [/SD_CLK] of U1 on F.Cu"]),
    v("diff_pair_gap_out_of_range", "Differential pair gap out of range (usb_gap maximum gap 0.2500 mm; actual 0.8000 mm)",
      ["Track [/USB_D_P] on F.Cu, length 1.0000 mm", "Track [/USB_D_N] on F.Cu, length 1.0000 mm"]),
    v("diff_pair_uncoupled_length_too_long", "Differential uncoupled length too long (usb_gap maximum uncoupled length "
      "3.0000 mm; actual 5.0556 mm)", ["Track [/USB_D_P] on F.Cu, length 11.7000 mm", "Track [/USB_D_N] on F.Cu, length 11.7000 mm"]),
    v("too_many_vias", "Too many vias on a connection (rule 'few' max count 1; actual 2)", ["Via [/M_A0] on F.Cu - B.Cu"]),
    v("clearance", "Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1000 mm)",
      ["Track [/A] on F.Cu", "Track [/B] on F.Cu"]),
    v("skew_out_of_range", "Skew zwischen Leiterbahnen ausserhalb (Regel 'x')", ["Pad 1 [/Q_IO0] of U1 on F.Cu"]),
    v("length_out_of_range", "Track length out of range (rule 'w' max length 8.0000 mm; actual 9.5000 mm)",
      ["Track [/XI] on F.Cu, length 9.5000 mm"], severity="warning"),
]


def test_kicad_markers_parse_the_five_formats_and_keep_what_does_not_parse():
    k = C.kicad_markers(RAW)
    assert k["counts"] == {"diff_pair_gap_out_of_range": 1, "diff_pair_uncoupled_length_too_long": 1,
                           "length_out_of_range": 2, "skew_out_of_range": 2, "too_many_vias": 1}
    assert k["warnings"] == {"length_out_of_range": 1} and k["unparsed"] == 1 and len(k["markers"]) == 8
    by = {(m["type"], tuple(m["nets"])): m for m in k["markers"]}
    a = by[("length_out_of_range", ("/A",))]
    assert (a["rule"], a["side"], a["limit"], a["actual"], a["len"]) == ("probe", "min", 2000.0, 31.065, 31.065)
    s = by[("skew_out_of_range", ("/SD_CLK",))]
    assert (s["rule"], s["limit"], s["actual"], s["target"], s["from"], s["len"]) == ("sd_match", 0.3, -3.0627, 33.0627,
                                                                                    "/SD_D3", 30.0)
    g = by[("diff_pair_gap_out_of_range", ("/USB_D_P", "/USB_D_N"))]
    assert (g["rule"], g["side"], g["limit"], g["actual"]) == ("usb_gap", "max", 0.25, 0.8)
    u = by[("diff_pair_uncoupled_length_too_long", ("/USB_D_P", "/USB_D_N"))]
    assert (u["rule"], u["limit"], u["actual"]) == ("usb_gap", 3.0, 5.0556)
    t = by[("too_many_vias", ("/M_A0",))]
    assert (t["rule"], t["limit"], t["actual"]) == ("few", 1.0, 2.0)
    # a message in another language: the type and the net still count, flagged unparsed
    q = by[("skew_out_of_range", ("/Q_IO0",))]
    assert q.get("unparsed") and "limit" not in q and k["nets"]["/Q_IO0"] == ["skew_out_of_range"]
    # KiCad's own lengths: the flagged net's, and the longest one a skew message names
    assert k["len"] == {"/A": 31.065, "/SD_D0": 0.0, "/SD_CLK": 30.0, "/SD_D3": 33.0627, "/XI": 9.5}
    assert "clearance" not in k["counts"] and "/B" not in k["nets"]
    assert C.kicad_markers(RAW, nets={"/SD_CLK"})["counts"] == {"skew_out_of_range": 1}
    assert C.kicad_markers([])["counts"] == {} and C.kicad_markers(None)["markers"] == []
    # kio's marker shape (message + item texts) parses the same way
    km = [{"type": r["type"], "message": r["description"], "items": [i["description"] for i in r["items"]]} for r in RAW[:1]]
    assert C.kicad_markers(km)["len"] == {"/A": 31.065}


# ── the report ────────────────────────────────────────────────────────────
def report_board():
    nets = ["/SD_D0", "/SD_D1", "/SD_CLK", "/USB_D_P", "/USB_D_N", "/XI", "/W"]
    return board([(ref, i + 1, n, x, 4.0 + 2 * i) for i, n in enumerate(nets) for ref, x in (("J1", 5.0), ("U1", 35.0))])


def report_pieces(d0=30.0, clk=33.0):
    """SD_D1 30 mm, SD_D0 and SD_CLK detoured to the given lengths; the pair side by side at 0.2; XI 30 mm."""
    def detour(net, y, total, first):
        h = (total - 30.0) / 2.0
        return seg(net, [(5, y), (10, y), (10, y - h), (12, y - h), (12, y), (35, y)], first=first) if h > 0 else \
            seg(net, [(5, y), (35, y)], first=first)
    pcs = detour("/SD_D0", 4.0, d0, 1) + seg("/SD_D1", [(5, 6), (35, 6)], first=20) + detour("/SD_CLK", 8.0, clk, 40)
    pcs += seg("/USB_D_P", [(5, 10), (6, 10), (6.8, 10.8), (33.2, 10.8), (34, 10), (35, 10)], first=60)
    pcs += seg("/USB_D_N", [(5, 12), (6, 12), (6.8, 11.2), (30, 11.2), (30, 13), (32, 13), (32, 11.2), (33.2, 11.2), (34, 12),
                            (35, 12)], first=80)
    pcs += seg("/XI", [(5, 14), (35, 14)], first=100) + seg("/W", [(5, 16), (20, 16)], w=0.2, first=110)
    pcs += seg("/W", [(20, 16), (35, 16)], w=0.15, first=120)
    return pcs


def test_report_says_by_how_much_and_never_passes_a_miss():
    b = report_board()
    dru = ('(rule sd (condition "A.NetName == \'/SD_*\'") (constraint skew (max 0.5mm)))\n'
           '(rule usb (condition "A.inDiffPair(\'/USB_D_\')") (constraint skew (max 0.15mm)) (constraint diff_pair_uncoupled (max 5mm)))\n'
           '(rule xi (condition "A.NetName == \'/XI\'") (constraint length (max 8mm)))')
    cons, _ = resolve(b, {"nets": {"/W": {"widths": {"F.Cu": 0.2}}}}, dru)
    rep = C.measure(ctx_of(b, cons, report_pieces()))
    assert rep["mode"] == "auto" and "kicad" not in rep                            # no judged check at hand
    g = rep["groups"][0]
    assert (g["name"], g["n"], g["src"], g["tol"], g["ok"]) == ("sd", 3, "dru", 0.5, False)
    assert (g["target"], g["min"], g["max"]) == (33.0, 30.0, 33.0)
    assert g["short_by"] == 2.5 and g["dev"] == -3.0 and g["worst"] in ("/SD_D0", "/SD_D1") and "long_by" not in g
    p = rep["pairs"][0]
    assert (p["p"], p["n"], p["src"], p["ok"]) == ("/USB_D_P", "/USB_D_N", "dru", False)
    assert abs(p["len"][1] - p["len"][0] - 3.6) < 1e-9 and p["skew"] == 3.6 and p["max_skew"] == 0.15
    assert p["skew_over"] == 3.45
    assert p["coupled"] < 100 and p["uncoupled"] > 5 and p["uncoupled_over"] > 0 and p["max_uncoupled"] == 5.0
    s = rep["short"][0]
    assert s == {"net": "/XI", "len": 30.0, "max": 8.0, "ok": False, "long_by": 22.0}
    assert rep["widths"] == {"n": 1, "off": [["/W", "F.Cu", 0.15, 0.2, 15.0]]}
    assert (rep["ok"], rep["missed"]) == (0, 4)
    assert rep["nets"]["/SD_CLK"] == [33.0, 33.0, 0.0, 0.0, 0] and set(rep["nets"]) == {
        "/SD_D0", "/SD_D1", "/SD_CLK", "/USB_D_P", "/USB_D_N", "/XI"}
    # inside the tolerance: ok, with the deviation still reported
    rep = C.measure(ctx_of(b, cons, report_pieces(d0=32.6, clk=33.0)))
    g = rep["groups"][0]
    assert g["ok"] is False and g["short_by"] == 2.5                                # /SD_D1 is still 30 mm
    cons2, _ = resolve(b, {"groups": [{"nets": ["/SD_D0", "/SD_CLK"], "tol": 0.5, "name": "two"},
                                      {"nets": ["/SD_D1"], "off": True}]}, "")
    rep = C.measure(ctx_of(b, cons2, report_pieces(d0=32.6, clk=33.0)))
    g = rep["groups"][0]
    assert (g["name"], g["ok"], g["dev"], g["src"]) == ("two", True, -0.4, "plan") and "short_by" not in g
    # an explicit target: too long is a miss as well, by how much
    cons3, _ = resolve(b, {"groups": [{"nets": ["/SD_D0", "/SD_CLK"], "tol": 0.5, "len": 31}], "diff_pairs": []}, "")
    g = C.measure(ctx_of(b, cons3, report_pieces(d0=30.0, clk=33.0)))["groups"][0]
    assert g["ok"] is False and g["short_by"] == 0.5 and g["long_by"] == 1.5 and g["target"] == 31.0
    # a member that is not completely routed: never ok, and it says why
    conns = [ConnV2(idx=0, net="/SD_CLK", a=Terminal(5, 8, (0,)), b=Terminal(35, 8, (0,)))]
    rep = C.measure(ctx_of(b, cons2, report_pieces(d0=32.6, clk=33.0), conns=conns, status={0: "failed"}))
    g = rep["groups"][0]
    assert g["ok"] is False and g["open"] == ["/SD_CLK"] and g["why"] == "open connection"
    # a name-detected group is information: ok None, never counted as a pass, and it names its nets for the patch
    cons4, _ = resolve(b, None, "")
    rep = C.measure(ctx_of(b, cons4, report_pieces()))
    assert "groups" not in rep or all(r["ok"] is None for r in rep["groups"])        # 2 data lines: no SD group at all
    assert rep["pairs"][0]["ok"] is None and rep["pairs"][0]["src"] == "name" and (rep["ok"], rep["missed"]) == (0, 0)


def test_report_takes_kicads_verdict_and_lengths_when_the_board_was_judged():
    b = report_board()
    dru = '(rule sd (condition "A.NetName == \'/SD_*\'") (constraint skew (max 0.5mm)))'
    cons, _ = resolve(b, {"diff_pairs": []}, dru)
    cx = ctx_of(b, cons, report_pieces(d0=32.8, clk=33.0))
    mine = C.measure(cx)["groups"][0]
    assert mine["ok"] is False and mine["short_by"] == 2.5
    # KiCad flags /SD_D1 and gives its own figures (here 0.004 mm off the router's): they are what is reported
    raw = [v("skew_out_of_range", "Skew between tracks out of range (rule 'sd' max skew 0.5000 mm; actual -3.0040 mm; "
             "target net length 33.0040 mm (from /SD_CLK); actual 30.0000 mm)", ["Track [/SD_D1] on F.Cu, length 30.0000 mm"])]
    rep = C.measure(cx, kicad={"raw": raw, "unconnected": {}})
    g = rep["groups"][0]
    assert g["ok"] is False and g["target"] == 33.004 and g["short_by"] == 2.504 and g["worst"] == "/SD_D1"
    assert rep["kicad"] == {"skew_out_of_range": 1} and rep["len_model_err"] == 0.004 and "kicad_other" not in rep
    # KiCad silent on a rule it checks itself = its verdict: pass (the router's figure is within 2.5 um of the edge)
    cx2 = ctx_of(b, cons, report_pieces(d0=33.0, clk=33.0) + seg("/SD_D1", [(35, 6), (36.2, 6), (35, 6.0001)], first=300))
    rep = C.measure(cx2, kicad={"raw": [], "unconnected": {}})
    assert rep["groups"][0]["ok"] is True and rep["kicad"] == {}
    # KiCad flags a net of a group the router's own figures pass: not ok, and it says so
    cons_p, _ = resolve(b, {"groups": [{"nets": ["/SD_D0", "/SD_CLK"], "tol": 0.5}], "diff_pairs": []}, "")
    cx3 = ctx_of(b, cons_p, report_pieces(d0=32.8, clk=33.0))
    raw = [v("skew_out_of_range", "Skew between tracks out of range (rule 'k' max skew 0.1000 mm; actual -0.2000 mm; "
             "target net length 33.0000 mm (from /SD_CLK); actual 32.8000 mm)", ["Track [/SD_D0] on F.Cu"])]
    g = C.measure(cx3, kicad={"raw": raw})["groups"][0]
    assert g["ok"] is False and "KiCad flags /SD_D0" in g["why"]
    # a KiCad constraint error no row explains is listed (finding KICAD_CONSTRAINT_ERRORS)
    raw = [v("too_many_vias", "Too many vias on a connection (rule 'few' max count 1; actual 2)", ["Via [/W] on F.Cu - B.Cu"])]
    rep = C.measure(cx3, kicad={"raw": raw})
    assert rep["kicad_other"] == [["too_many_vias", "/W"]] and rep["kicad"] == {"too_many_vias": 1}
    # KiCad's per-net unconnected count decides what is open
    rep = C.measure(cx3, kicad={"raw": [], "unconnected": {"/SD_D0": 1}})
    assert rep["groups"][0]["ok"] is False and rep["groups"][0]["open"] == ["/SD_D0"]


def test_members_of_a_tuned_group_are_ordered_early_and_nothing_else_moves():
    """Design §14.6: priority max(own, 70) for the nets of a group that may get copper — and only for those: a
    name-detected (report) group, 'report' mode and a board without constraints leave every rank as it was. The
    rule is switched on by the length tuner being built (classify._tuner_built): without one, the order of every
    board is exactly what it was before constraints existed."""
    import dataclasses
    from copper_kicad.pcb.v2 import classify
    nets = ["/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3", "/SD_CLK", "/A", "/B"]
    b = board(two_parts(nets))
    real = classify._tuner_built
    try:
        classify._tuner_built = lambda: False
        _tuned_group_order(b, classify, dataclasses, built=False)
        classify._tuner_built = lambda: True
        _tuned_group_order(b, classify, dataclasses, built=True)
    finally:
        classify._tuner_built = real


def _tuned_group_order(b, classify, dataclasses, built):

    def ranks(plan, cons="resolve"):
        cfg = P.resolve(b, plan)
        if cons == "resolve":
            cfg.x.cons = C.resolve(b, cfg, cfg.x, C.ProjectRules())
        else:
            cfg.x.cons = cons
        return {c.net: c.rank for c in classify.connections(b, cfg, cfg.x, {})}, cfg
    base, _ = ranks(None, cons=None)                                  # the router before constraints existed
    assert base["/A"][1] == base["/B"][1] == base["/SD_D0"][1] == -50.0 and base["/SD_CLK"][1] == -85.0
    named, cfg = ranks(None)                                          # names alone: an SD group in report mode
    assert [g.mode for g in cfg.x.cons.groups] == ["report"] and named == base
    tuned, cfg = ranks({"groups": [{"nets": ["/A", "/SD_CLK"], "tol": 0.5}]})
    assert cfg.x.cons.tuned()
    if not built:
        assert tuned == base                                          # no tuner: nothing moves, whatever the plan says
        return
    assert tuned["/A"][1] == -classify.TUNED_PRIORITY == -70.0
    assert tuned["/SD_CLK"][1] == -85.0                               # already above 70: unchanged
    # (step B: the members also share one order key and sort longest first inside it - tests/test_v2_tune.py;
    # every net outside the group keeps its rank exactly)
    assert tuned["/A"][3] == tuned["/SD_CLK"][3] == base["/A"][3] and tuned["/A"][:3] == (0, -70.0, base["/A"][2])
    others = lambda d: {n: r for n, r in d.items() if n not in ("/A", "/SD_CLK")}   # noqa: E731
    assert others(tuned) == others(base)
    rep, cfg = ranks({"global": {"constraints": "report"}, "groups": [{"nets": ["/A", "/B"], "tol": 0.5}]})
    assert rep == base and cfg.x.cons.groups[0].mode == "tune" and cfg.x.cons.tuned() == ()
    off, _ = ranks({"global": {"constraints": "off"}, "groups": [{"nets": ["/A", "/B"], "tol": 0.5}]})
    assert off == base
    assert dataclasses.is_dataclass(cfg.x.cons) and dataclasses.replace(cfg.x.cons, mode="auto").tuned()


def test_no_clock_no_random():
    src = Path(C.__file__).read_text(encoding="utf-8")
    for banned in ("import time", "from time", "import random", "datetime", "perf_counter", "monotonic"):
        assert banned not in src, banned
    assert not getattr(C, "STUB", False)


if __name__ == "__main__":
    fails = 0
    tests = [(k, v_) for k, v_ in list(globals().items()) if k.startswith("test_") and callable(v_)]
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {name}: {e}")
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
