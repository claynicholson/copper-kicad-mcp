"""Router v2, S0 classify (design §4.3): roles, widths, buses, decoupling pairs, Steiner-ordered connections.

Pure tests on synthetic boards (no KiCad) + the benchmark corpus (extracts are cached under
bench/route/.work/v2_w5; made with KiCad's pcbnew when missing, skipped when that is not possible).

Run:  python tests/test_v2_classify.py            every test
      python tests/test_v2_classify.py test_name  one test
Scratch files go to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench" / "route"))
import common  # noqa: E402  (bench/route/common.py)

common.sandbox()       # KICAD_CONFIG_HOME, COPPER_HOME and the temp dir -> bench/route/.work, before copper_kicad loads
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import model, plan  # noqa: E402
from copper_kicad.pcb.v2 import classify as C  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2.types import ROLE_ORDER, Budget, Ctx  # noqa: E402

WORK = common.WORK / "v2_w5"
CORPUS = ("cm5_minima", "coldfire", "complex_hierarchy", "ecc83", "interf_u", "multichannel_mixer", "one_air_max",
          "phone_breathalyzer", "pic_programmer", "sonde_xilinx", "stickhub", "synth_bus_cross", "synth_qfp_fanout",
          "tinytapeout")


# ── synthetic boards (no KiCad) ───────────────────────────────────────────
def board(pads=(), *, size=(60.0, 40.0), origin=(0.0, 0.0), layers=("F.Cu", "B.Cu"), tracks=(), vias=(), zones=(),
          nets=None, rules=None):
    """pads: dicts {ref, num='1', net, x, y, hw=0.4, hh=0.4, r=0, rot=0, kind='smd'|'tht', layer=0, hole, c, fn, pt}.
    Every pad is its own connectivity cluster unless ``c`` says otherwise. nets: {name: {class, width, clearance}}.
    tracks: [net, layer, x1, y1, x2, y2, w, cluster, locked]; zones: see ``pour()``."""
    names = sorted({p["net"] for p in pads if p.get("net")} | set(nets or {}) | {t[0] for t in tracks}
                   | {z["net"] for z in zones if z.get("net")})
    R = {"clearance": 0.1, "track_width": 0.1, "via": 0.4, "drill": 0.2, "edge_clearance": 0.3, "hole_clearance": 0.25,
         "hole_to_hole": 0.25, "annular": 0.05,
         "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.25, "dp_width": 0.2}}
    R.update(rules or {})
    x0, y0 = origin
    ex = {"layers": list(layers), "rules": R,
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.25, "dp_width": 0.2, **((nets or {}).get(n) or {})} for i, n in enumerate(names)},
          "outline": {"closed": True, "polys": [{"o": [[x0, y0], [x0 + size[0], y0], [x0 + size[0], y0 + size[1]],
                                                       [x0, y0 + size[1]]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [list(t) for t in tracks], "vias": [list(v) for v in vias],
          "zones": [dict(z) for z in zones], "mask_openings": [],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    allL = list(range(len(layers)))
    for i, p in enumerate(pads):
        kind = p.get("kind", "smd")
        lays = allL if kind == "tht" else [p.get("layer", 0)]
        a = math.radians(p.get("rot", 0.0))
        d = {"ref": p["ref"], "num": str(p.get("num", "1")), "net": p.get("net", ""), "x": p["x"], "y": p["y"],
             "kind": kind, "layers": lays, "c": p.get("c", 1000 + i)}
        d["shapes"] = [[lays, "box", [p["x"], p["y"], round(math.cos(a), 6), round(-math.sin(a), 6), p.get("hw", 0.4),
                                      p.get("hh", 0.4), p.get("r", 0.0)]]]
        if kind == "tht":
            h = p.get("hole", 0.4)
            d["hole"] = [p["x"], p["y"], 1.0, 0.0, h, h]
        for k in ("fn", "pt"):
            if p.get(k):
                d[k] = p[k]
        ex["pads"].append(d)
    return model.Board(ex)


def pour(net, layer, rect, *, filled=False, islands=()):
    """A copper zone of ``net`` on ``layer`` over rect [x0, y0, x1, y1]; ``islands``: fill polygons (point lists)."""
    x0, y0, x1, y1 = rect
    z = {"name": f"{net} pour", "layers": [layer], "net": net, "priority": 0, "filled": bool(filled or islands),
         "polys": [{"o": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}]}
    if islands:
        z["fills"] = {str(layer): [{"o": [list(p) for p in isl]} for isl in islands]}
    return z


class NX:
    def __init__(self, **kw):
        self.role = kw.get("role")
        self.tree = kw.get("tree")


class X:
    """Stand-in for planx.V2Cfg (only what classify / power read)."""

    def __init__(self, nets=None, buses=None, reserve=None, power_width_factor=2.0):
        self.nets = {n: NX(**d) for n, d in (nets or {}).items()}
        self.buses = list(buses or [])
        self.reserve = list(reserve or [])
        self.power_width_factor = power_width_factor
        self.auto = {"roles": {}, "buses": [], "widths": {}, "layer_dirs": {}}


def P(ref, num, net, x, y, **kw):
    return {"ref": ref, "num": num, "net": net, "x": x, "y": y, **kw}


def header(ref, nets, x, y, *, pitch=2.54, vertical=True, first=1, **kw):
    """A single-row connector: one pad per net, pad numbers ascending along the row."""
    out = []
    for i, n in enumerate(nets):
        out.append(P(ref, first + i, n, x + (0 if vertical else i * pitch), y + (i * pitch if vertical else 0), **kw))
    return out


def conns_of(b, plan_=None, x=None):
    cfg = plan.resolve(b, plan_)
    return C.connections(b, cfg, x, C.roles(b, cfg, x)), cfg


def ends(c):
    return (c.a.pad, c.b.pad)


# ── roles ─────────────────────────────────────────────────────────────────
def _role_board():
    pads = []
    pads += header("J1", ["/D0", "/D1", "/D2", "/D3", "/USB_DP", "/USB_DN"], 10, 5)
    pads += header("U1", ["/D0", "/D1", "/D2", "/D3", "/USB_DP", "/USB_DN"], 30, 5, pitch=1.27)
    pads += [P("U1", 20, "GND", 34, 5), P("C1", 2, "GND", 40, 5), P("U1", 21, "+3V3", 34, 8), P("C1", 1, "+3V3", 40, 8),
             P("Y1", 1, "/XTAL1", 34, 12), P("U1", 22, "/XTAL1", 34, 14, fn="XTAL1"),
             P("R1", 1, "/SIG", 45, 20), P("R2", 1, "/SIG", 50, 20),
             P("R3", 1, "VSYS", 45, 25), P("R4", 1, "VSYS", 50, 25),
             P("R5", 1, "/OUT", 45, 30), P("U2", 1, "/OUT", 50, 30, pt="power_out")]
    return board(pads, zones=[pour("GND", 1, (0, 0, 60, 40))],
                 nets={"VSYS": {"class": "Default"}, "/SIG": {"class": "Default"}})


def test_role_precedence():
    b = _role_board()
    cfg = plan.resolve(b, None)
    r = C.roles(b, cfg, None)
    assert r["GND"] == "plane"                                   # a pour beats the ground name
    assert r["+3V3"] == "power" and r["VSYS"] == "power"         # power names
    assert r["/OUT"] == "power"                                  # a power-output pin
    assert r["/USB_DP"] == "pair" and r["/USB_DN"] == "pair"     # declared (default plan) pair beats bus membership
    assert [r[n] for n in ("/D0", "/D1", "/D2", "/D3")] == ["bus"] * 4
    assert r["/XTAL1"] == "critical"                             # keep_short of the default plan
    assert r["/SIG"] == "signal"
    assert set(r.values()) <= set(ROLE_ORDER)
    # the plan role beats everything, in both directions
    x = X(nets={"/SIG": {"role": "critical"}, "GND": {"role": "power"}, "/D0": {"role": "signal"},
                "+3V3": {"role": "signal"}})
    r2 = C.roles(b, cfg, x)
    assert r2["/SIG"] == "critical" and r2["GND"] == "power" and r2["/D0"] == "signal" and r2["+3V3"] == "signal"
    assert r2["/D1"] == "bus" and r2["VSYS"] == "power"
    # an unknown role value is ignored, a plain dict works as x, and x = None is the default
    assert C.roles(b, cfg, X(nets={"/SIG": {"role": "nonsense"}}))["/SIG"] == "signal"
    assert C.roles(b, cfg, {"nets": {"/SIG": {"role": "bus"}}})["/SIG"] == "bus"
    # power by net class name
    b2 = board([P("R1", 1, "/RAIL", 5, 5), P("R2", 1, "/RAIL", 9, 5)], nets={"/RAIL": {"class": "Power_5A"}})
    assert C.roles(b2, plan.resolve(b2, None), None)["/RAIL"] == "power"
    # a pair is a pair only when the plan (default plan included) declares it
    assert C.roles(b, plan.resolve(b, {"diff_pairs": []}), None)["/USB_DP"] == "bus"


def test_real_v2cfg_is_understood():
    from copper_kicad.pcb.v2 import planx
    if getattr(planx, "STUB", False) or not hasattr(planx, "NetX"):
        return "skipped: planx is a stub"
    b = _role_board()
    cfg = plan.resolve(b, None)
    x = planx.V2Cfg()
    nx = planx.NetX()
    nx.role, nx.tree = "critical", "star"
    x.nets["/SIG"] = nx
    x.power_width_factor = 3.0
    assert C.roles(b, cfg, x)["/SIG"] == "critical"
    assert C._net_hint(x, "/SIG", "tree") == "star" and C._net_hint(x, "/D0", "tree") is None
    assert C._hint(x, "power_width_factor") == 3.0


def test_analog_and_kind_detection():
    b = board([P("U1", 1, "GNDA", 5, 5), P("C1", 1, "GNDA", 9, 5), P("U1", 2, "/ADC_IN3", 5, 8), P("R1", 1, "/ADC_IN3", 9, 8),
               P("U1", 3, "AVDD", 5, 11), P("C2", 1, "AVDD", 9, 11), P("U1", 4, "/LED", 5, 14), P("R2", 1, "/LED", 9, 14)])
    assert C.net_kind(b, "GNDA") == "ground" and C.net_kind(b, "AVDD") == "power" and C.net_kind(b, "/LED") == ""
    assert [C.is_analog(b, n) for n in ("GNDA", "/ADC_IN3", "AVDD", "/LED")] == [True, True, True, False]
    t = C.net_table(b, plan.resolve(b, None), None)
    assert t["GNDA"]["analog"] and t["GNDA"]["role"] == "power" and not t["/LED"]["analog"]


def test_gnda_is_routed_not_skipped():
    pads = [P(f"C{i}", 1, "GNDA", 5 + 4 * i, 10) for i in range(7)] + [P("R1", 1, "/S", 5, 20), P("R2", 1, "/S", 9, 20)]
    b = board(pads)
    cfg = plan.resolve(b, None)
    assert cfg.nets["GNDA"].skip and not cfg.nets["GNDA"].explicit          # v1 would leave it for a pour
    # with no plan it is left for the pour under v2 as well (the tool's promise), and reported with its count
    ctx0 = Ctx(board=b, cfg=cfg, x=X())
    rep0 = C.run(ctx0, Budget(1000))
    assert [c.net for c in ctx0.conns] == ["/S"] and ctx0.x.auto["unskipped"] == []
    assert [(s["net"], s["connections"]) for s in rep0.data["skipped"]] == [("GNDA", 6)]
    assert "pcb_pour(net='GNDA')" in rep0.data["skipped"][0]["reason"]
    # the plan says "route it" (skip: false; planx.v2_plan writes exactly this): routed, as power, late
    cfg = plan.resolve(b, {"nets": {"GNDA": {"skip": False}}})
    cs = C.connections(b, cfg, None, C.roles(b, cfg, None))
    g = [c for c in cs if c.net == "GNDA"]
    assert len(g) == 6 and all(c.role == "power" for c in g)                # routed, as power
    order = sorted(cs, key=lambda c: c.rank)
    assert order[0].net == "/S" and all(c.net == "GNDA" for c in order[1:])  # ... and late
    ctx = Ctx(board=b, cfg=cfg, x=X())
    rep = C.run(ctx, Budget(1000))
    assert ctx.x.auto["unskipped"] == ["GNDA"] and rep.data["skipped"] == []
    # a net the session did not select (nets=[...]) is not routed either, and not listed as skipped by the plan
    cfg4 = plan.resolve(b, None)
    cfg4.nets["/S"].skip, cfg4.nets["/S"].skip_reason = True, "not selected"
    ctx4 = Ctx(board=b, cfg=cfg4, x=X())
    C.run(ctx4, Budget(1000))
    assert ctx4.conns == []
    # an explicit plan skip is honoured and reported
    cfg2 = plan.resolve(b, {"nets": {"GNDA": {"skip": True}}})
    ctx2 = Ctx(board=b, cfg=cfg2, x=X())
    rep2 = C.run(ctx2, Budget(1000))
    assert [c.net for c in ctx2.conns] == ["/S"]
    assert rep2.data["skipped"] == [{"net": "GNDA", "connections": 6, "reason": "plan: skip"}]
    # and "skip: false" is simply routed
    cfg3 = plan.resolve(b, {"nets": {"GNDA": {"skip": False}}})
    assert len([c for c in C.connections(b, cfg3, None, {}) if c.net == "GNDA"]) == 6


# ── buses ─────────────────────────────────────────────────────────────────
def _bus_board(order_b, n=6, extra=()):
    nets = [f"/D{i}" for i in range(n)]
    pads = header("J1", nets, 10, 5) + header("U1", [nets[i] for i in order_b], 30, 5, pitch=1.27)
    return board(pads + list(extra))


def test_bus_detection_positive():
    b = _bus_board(range(6))
    bl = C.detect_buses(b)
    assert len(bl) == 1
    d = bl[0]
    assert d["nets"] == [f"/D{i}" for i in range(6)] and d["refs"] == ["J1", "U1"] and d["pad_order"] == "same" and d["auto"]
    assert d["pads"] == [[f"J1.{i}" for i in range(1, 7)], [f"U1.{i}" for i in range(1, 7)]]
    assert d["pitch"] == [2.54, 1.27]
    assert C.detect_buses(b) == bl                                           # same twice
    r = C.roles(b, plan.resolve(b, None), None)
    assert all(r[n] == "bus" for n in d["nets"])


def test_bus_detection_reversed():
    b = _bus_board([5, 4, 3, 2, 1, 0])
    bl = C.detect_buses(b)
    assert len(bl) == 1 and bl[0]["pad_order"] == "reversed"
    assert bl[0]["nets"] == [f"/D{i}" for i in range(6)]
    assert bl[0]["pads"][1] == [f"U1.{i}" for i in range(6, 0, -1)]


def test_bus_detection_negative():
    assert C.detect_buses(_bus_board([2, 0, 5, 1, 4, 3])) == []              # scrambled order: not a bus
    assert C.detect_buses(_bus_board(range(3), n=3)) == []                   # three nets are not a bus
    # a net with a pad on a third footprint (a pull-up) is never a member: /D2 splits the run into 2 + 3
    b = _bus_board(range(6), extra=[P("R9", 1, "/D2", 20, 30)])
    assert C.detect_buses(b) == []
    # ... but with 9 nets the 6 after the tapped one still form a bus
    b = _bus_board(range(9), n=9, extra=[P("R9", 1, "/D2", 20, 30)])
    bl = C.detect_buses(b)
    assert len(bl) == 1 and bl[0]["nets"] == [f"/D{i}" for i in range(3, 9)]
    # ground / power / poured nets never take part
    nets = ["GND", "+5V", "VCC", "GNDA", "/A", "/B"]
    b = board(header("J1", nets, 10, 5) + header("U1", nets, 30, 5))
    assert C.detect_buses(b) == []
    # pads that are not neighbours on one of the parts (every other pin) are not consecutive
    nets = [f"/D{i}" for i in range(5)]
    pads = header("J1", nets, 10, 5)
    for i, n in enumerate(nets):
        pads.append(P("U1", 2 * i + 1, n, 30, 5 + 2 * i * 1.27))
        pads.append(P("U1", 2 * i + 2, f"/X{i}", 30, 5 + (2 * i + 1) * 1.27))
        pads.append(P("R%d" % i, 1, f"/X{i}", 40, 5 + 3 * i))
    assert C.detect_buses(board(pads)) == []


def test_declared_bus_wins_over_detection():
    b = _bus_board(range(6))
    x = X(buses=[{"nets": ["/D0", "/D1"], "order": ["/D1", "/D0"], "layer": 0, "spacing": 0.5}])
    bl = C.buses(b, x)
    assert bl[0] == {"nets": ["/D1", "/D0"], "auto": False, "layer": 0, "spacing": 0.5}
    assert len(bl) == 1                                                      # the auto bus shares nets with it: dropped
    cfg = plan.resolve(b, None)
    r = C.roles(b, cfg, x)
    assert r["/D0"] == "bus" and r["/D1"] == "bus" and r["/D2"] == "signal"


def test_dual_row_and_rotated_rows():
    # two buses between the same two parts, one per row; rows at 30 degrees on the second part
    a = [f"/A{i}" for i in range(5)]
    c = [f"/C{i}" for i in range(5)]
    pads = header("J1", a, 10, 5) + header("J1", c, 12.54, 5, first=11)
    ca, sa = math.cos(math.radians(30)), math.sin(math.radians(30))
    for i in range(5):
        pads.append(P("U1", 1 + i, a[i], 30 + i * 1.0 * ca, 10 + i * 1.0 * sa))
        pads.append(P("U1", 11 + i, c[4 - i], 30 + i * 1.0 * ca - 1.0 * sa, 10 + i * 1.0 * sa + 1.0 * ca))
    bl = C.detect_buses(board(pads))
    assert sorted((d["nets"], d["pad_order"]) for d in bl) == [(a, "same"), (c, "reversed")]


# ── widths ────────────────────────────────────────────────────────────────
def _width_board(pad_hh=0.4, cls_w=0.2):
    return board([P("U1", 1, "+5V", 5, 5, hw=0.8, hh=pad_hh), P("C1", 1, "+5V", 12, 5, hw=0.6, hh=0.6),
                  P("R1", 1, "/S", 5, 15), P("R2", 1, "/S", 12, 15)],
                 nets={"+5V": {"width": cls_w}})


def test_width_clamps():
    def w(b, plan_=None, x=None):
        cfg = plan.resolve(b, plan_)
        return C.widths(b, cfg, x, C.roles(b, cfg, x))
    # class x 2
    assert w(_width_board(0.4, 0.2))["+5V"] == (0.4, 0.2)
    # ... capped at 0.8 x the smallest pad minor dimension (0.3 mm pad -> 0.24)
    assert w(_width_board(0.15, 0.2))["+5V"] == (0.24, 0.2)
    # ... and at 1.0 mm
    b = board([P("U1", 1, "+5V", 5, 5, hw=2, hh=2), P("C1", 1, "+5V", 15, 5, hw=2, hh=2)], nets={"+5V": {"width": 0.8}})
    assert w(b)["+5V"] == (1.0, 0.2)                                         # neck: the Default-class width (v1's rule)
    # never below the class width, whatever the pads say
    assert w(_width_board(0.1, 0.5))["+5V"] == (0.5, 0.2)
    # the neck is never above the net's own class width, nor below the board minimum
    b = board([P("U1", 1, "+5V", 5, 5), P("C1", 1, "+5V", 15, 5)], nets={"+5V": {"width": 0.15}})
    assert w(b)["+5V"] == (0.3, 0.15)
    b = board([P("U1", 1, "+5V", 5, 5), P("C1", 1, "+5V", 15, 5)], nets={"+5V": {"width": 0.5}}, rules={"track_width": 0.25})
    assert w(b)["+5V"] == (0.64, 0.25)
    # floored to 0.01 mm
    b = board([P("U1", 1, "+5V", 5, 5, hw=0.4, hh=0.17), P("C1", 1, "+5V", 15, 5)])
    assert w(b)["+5V"] == (0.27, 0.2)                                        # 0.8 x 0.34 = 0.272
    # power_width_factor from the plan hints; a factor below 1 never narrows
    assert w(_width_board(0.4, 0.2), x=X(power_width_factor=3.0))["+5V"] == (0.6, 0.2)
    assert w(_width_board(0.4, 0.2), x=X(power_width_factor=0.5))["+5V"] == (0.2, 0.2)
    # a plan width wins (neck = class width); neck: false keeps the width everywhere
    assert w(_width_board(), {"nets": {"+5V": {"width": 0.7}}})["+5V"] == (0.7, 0.2)
    assert w(_width_board(), {"nets": {"+5V": {"width": 0.7, "neck": False}}})["+5V"] == (0.7, 0.7)
    assert w(_width_board(), {"nets": {"+5V": {"neck": False}}})["+5V"] == (0.4, 0.4)
    # signals: the class width; a plan width on a signal necks to the class width
    assert w(_width_board())["/S"] == (0.2, 0.2)
    assert w(_width_board(), {"nets": {"/S": {"width": 0.5}}})["/S"] == (0.5, 0.2)
    # connections carry width / neck / clearance / via of their net
    b = _width_board(0.15, 0.2)
    cs, cfg = conns_of(b, {"nets": {"/S": {"clearance": 0.35}}})
    c5 = next(c for c in cs if c.net == "+5V")
    cS = next(c for c in cs if c.net == "/S")
    assert (c5.width, c5.neck, c5.role) == (0.24, 0.2, "power") and c5.via == (0.6, 0.3) and c5.clearance == 0.2
    assert cS.clearance == 0.35 and (cS.width, cS.neck) == (0.2, 0.2)


# ── connections ───────────────────────────────────────────────────────────
def test_steiner_order_and_estimate():
    # R and C attach first (10 mm); D is 14.07 from either pad but only 12 from the planned R-C run
    b = board([P("R", 1, "/N", 10, 5), P("C", 1, "/N", 20, 5), P("D", 1, "/N", 15, 17)])
    cs, _cfg = conns_of(b)
    assert [c.idx for c in cs] == [0, 1]
    assert ends(cs[0]) == ("R.1", "C.1") or ends(cs[0]) == ("C.1", "R.1")
    assert cs[0].est == 10.0 and cs[0].tree_target
    assert cs[1].a.pad == "D.1" and abs(cs[1].est - 12.0) < 1e-9 and cs[1].tree_target
    assert cs[1].b.pad in ("R.1", "C.1")                                    # a real pad of the tree is the anchor
    # 'mst': pad-to-pad estimates
    cm, _cfg = conns_of(b, x=X(nets={"/N": {"tree": "mst"}}))
    assert abs(cm[1].est - (12 + 5 * (math.sqrt(2) - 1))) < 1e-6 and cm[1].tree_target
    # terminals: quantised pad centres with layers, pad name and cluster
    t = cs[1].a
    assert (t.x, t.y, t.layers, t.pad, t.escape) == (15.0, 17.0, (0,), "D.1", False) and t.cluster > 0


def test_steiner_uses_kept_copper_and_starts_at_the_largest_cluster():
    # cluster 7 = two pads joined by a kept track along y = 10; pad B hangs 3 mm below the middle of that track
    pads = [P("A", 1, "/N", 0 + 5, 10, c=7), P("A", 2, "/N", 25, 10, c=7), P("B", 1, "/N", 15, 13, c=8),
            P("Z", 1, "/N", 40, 30, c=9)]
    b = board(pads, tracks=[["/N", 0, 5, 10, 25, 10, 0.2, 7, 0]])
    cs, _cfg = conns_of(b)
    assert len(cs) == 2
    assert cs[0].a.pad == "B.1" and abs(cs[0].est - 3.0) < 1e-9             # to the track, not to a pad (10.4 mm)
    assert cs[0].b.pad in ("A.1", "A.2")
    assert cs[1].a.pad == "Z.1"                                             # the far pad comes last
    # the root is the largest cluster even when it sorts last
    pads = [P("A", 1, "/N", 5, 5, c=3), P("B", 1, "/N", 30, 5, c=9), P("B", 2, "/N", 31, 5, c=9), P("B", 3, "/N", 32, 5, c=9)]
    cs, _cfg = conns_of(board(pads))
    assert len(cs) == 1 and cs[0].a.pad == "A.1" and cs[0].b.pad == "B.1"


def test_tree_overrides():
    pads = [P("H", 1, "/N", 20, 20, c=1), P("H", 2, "/N", 21, 20, c=1),              # the hub (largest cluster)
            P("A", 1, "/N", 30, 20), P("B", 1, "/N", 38, 20), P("D", 1, "/N", 46, 20)]
    b = board(pads)
    star, _ = conns_of(b, x=X(nets={"/N": {"tree": "star"}}))
    assert [ends(c) for c in star] == [("A.1", "H.2"), ("B.1", "H.2"), ("D.1", "H.2")]
    assert not any(c.tree_target for c in star)
    chain, _ = conns_of(b, x=X(nets={"/N": {"tree": "chain"}}))
    assert [ends(c) for c in chain] == [("A.1", "H.2"), ("B.1", "A.1"), ("D.1", "B.1")]
    assert not any(c.tree_target for c in chain)
    mst, _ = conns_of(b, x=X(nets={"/N": {"tree": "mst"}}))
    assert [ends(c) for c in mst] == [("A.1", "H.2"), ("B.1", "A.1"), ("D.1", "B.1")] and all(c.tree_target for c in mst)
    default, _ = conns_of(b)
    assert all(c.tree_target for c in default) and [c.a.pad for c in default] == ["A.1", "B.1", "D.1"]
    # an unknown tree value falls back to the default
    odd, _ = conns_of(b, x=X(nets={"/N": {"tree": "banana"}}))
    assert odd == default


def test_connections_are_deterministic_and_order_independent():
    import random
    rnd = random.Random(7)                       # test data only: the router itself never uses randomness
    pads = []
    for n in range(12):
        for k in range(rnd.randint(2, 9)):
            pads.append(P(f"X{n}_{k}", 1, f"/N{n:02d}", round(rnd.uniform(3, 57), 2), round(rnd.uniform(3, 37), 2)))
    b1 = board(pads)
    cs1, cfg1 = conns_of(b1)
    cs2, _ = conns_of(board(pads))
    assert cs1 == cs2 and [c.rank for c in cs1] == [c.rank for c in cs2]            # same twice
    shuffled = list(pads)
    rnd.shuffle(shuffled)
    # (cluster ids follow the canonical pad order of the extract, so only the 'c' hint must not travel with the dict)
    cs3, _ = conns_of(board(shuffled))
    assert [(c.net, c.a.pad, c.b.pad, c.est) for c in cs3] == [(c.net, c.a.pad, c.b.pad, c.est) for c in cs1]
    # every net is spanned: n pads -> n - 1 connections, every pad reachable from the first
    by = {}
    for c in cs1:
        by.setdefault(c.net, []).append(c)
    for name, net in b1.nets.items():
        assert len(by[name]) == len(net.pads) - 1
        seen = {by[name][0].b.pad}
        for c in by[name]:
            assert c.b.pad in seen and c.a.pad not in seen                 # each one attaches a new pad to the tree
            seen.add(c.a.pad)
        assert len(seen) == len(net.pads)
    assert [c.idx for c in cs1] == list(range(len(cs1)))
    # the Steiner estimate never exceeds the pad-to-pad distance
    for c in cs1:
        assert c.est <= C._octile(c.a.x - c.b.x, c.a.y - c.b.y) + 1e-6


def test_rank_order():
    b = _role_board()
    cs, cfg = conns_of(b)
    order = sorted(cs, key=lambda c: c.rank)
    roles = [c.role for c in order]
    # v1 priorities (crystal 85 + 8 > pair 90 > signal 50 > power 40 > ground 20), then the role order of §4.3
    first = {r: roles.index(r) for r in set(roles)}
    assert first["critical"] < first["pair"] < first["signal"] < first["power"] < first["plane"]
    assert first["bus"] < first["signal"]                                    # equal priority: bus before signal
    assert roles[-1] == "plane"
    for c in cs:
        nc = cfg.nets[c.net]
        if not (nc.dp_partner and nc.dp_role == "n"):
            assert c.rank == (nc.rank, -nc.priority, ROLE_ORDER[c.role], round(c.est, 3), c.net, c.idx)
    # the N net of a pair follows its P net directly
    names = [c.net for c in order]
    assert names.index("/USB_DN") == names.index("/USB_DP") + 1
    # plan order wins over everything
    cs2, _ = conns_of(b, {"order": ["GND", "*"]})
    assert sorted(cs2, key=lambda c: c.rank)[0].net == "GND"
    # short first inside one role and priority
    b3 = board([P("A", 1, "/L", 5, 5), P("B", 1, "/L", 35, 5), P("A", 2, "/M", 5, 10), P("B", 2, "/M", 9, 10)])
    cs3, _ = conns_of(b3)
    assert [c.net for c in sorted(cs3, key=lambda c: c.rank)] == ["/M", "/L"]


def test_plane_connections_have_an_open_target():
    from copper_kicad.pcb.v2 import pieces
    pads = [P("U1", 1, "GND", 10, 10, c=1), P("U1", 2, "GND", 11, 10, c=1), P("C1", 2, "GND", 20, 10), P("C2", 2, "GND", 30, 12)]
    b = board(pads, zones=[pour("GND", 1, (0, 0, 60, 40))])
    cs, _cfg = conns_of(b)
    assert len(cs) == 2 and all(c.role == "plane" and c.tree_target for c in cs)
    assert [c.a.pad for c in cs] == ["C1.2", "C2.2"]                         # every cluster but the main one
    for c in cs:
        assert c.b.pad is None and c.b.cluster == 0 and c.b.layers == ()     # "anything else of this net"
        if not getattr(pieces, "STUB", False):
            assert pieces.is_open(c.b)
    assert (cs[0].b.x, cs[0].b.y) == (11.0, 10.0) and abs(cs[0].est - 9.0) < 1e-9
    assert (cs[1].b.x, cs[1].b.y) == (20.0, 10.0)                            # the nearest other copper of the net


def test_decoupling_pairs_come_first():
    pads = []
    for i in range(4):
        pads.append(P("U1", i + 1, ["+3V3", "/A", "/B", "GND"][i], 20, 10 + i * 1.27, hw=0.6, hh=0.25))
    pads += [P("C1", 1, "+3V3", 17.5, 10, hw=0.4, hh=0.45), P("C1", 2, "GND", 16, 10, hw=0.4, hh=0.45),      # 2.5 mm from U1.1
             P("C2", 1, "+3V3", 30, 30), P("C2", 2, "GND", 31.5, 30),                                       # far away
             P("J1", 1, "+3V3", 5, 30), P("J1", 2, "GND", 5, 32), P("R1", 1, "/A", 40, 10), P("R2", 1, "/B", 40, 14)]
    b = board(pads, zones=[pour("GND", 1, (0, 0, 60, 40))])
    cfg = plan.resolve(b, None)
    r = C.roles(b, cfg, None)
    assert C.decoupling(b, r) == [("+3V3", "C1.1", "U1.1")]
    ctx = Ctx(board=b, cfg=cfg, x=X())
    rep = C.run(ctx, Budget(1000))
    d = ctx.history["classify"]["decaps"]
    assert len(d) == 1 and rep.data["decaps"] == d
    c = ctx.conns[d[0]]
    assert ends(c) == ("C1.1", "U1.1") and not c.tree_target and c.role == "power" and c.width > c.neck
    v = [k for k in ctx.conns if k.net == "+3V3"]
    assert v[0] is c and len(v) == 3                                         # first of its net, the net still spanned
    assert ctx.x.auto["decoupling"] == [["+3V3", "C1.1", "U1.1"]]
    # ground without a pour still makes the capacitor a decoupling capacitor; a 3-pad part is not an IC
    b2 = board(pads)
    assert C.decoupling(b2, C.roles(b2, plan.resolve(b2, None), None)) == [("+3V3", "C1.1", "U1.1")]
    b3 = board([p for p in pads if not (p["ref"] == "U1" and p["num"] == 4)])
    assert C.decoupling(b3, C.roles(b3, plan.resolve(b3, None), None)) == []


def test_run_fills_ctx():
    b = _role_board()
    cfg = plan.resolve(b, {"nets": {"/SIG": {"clearance": 0.4}}})
    conns = []
    ctx = Ctx(board=b, cfg=cfg, x=X(), oracle=O.Oracle(b), conns=conns)
    bud = Budget(10_000)
    rep = C.run(ctx, bud)
    assert ctx.conns is conns and len(conns) > 0                             # filled in place (RouteState holds the list)
    assert [c.idx for c in conns] == list(range(len(conns)))
    assert rep.name == "classify" and rep.routed == 0 and rep.failed == 0 and rep.data["connections"] == len(conns)
    assert bud.used == rep.work > 0
    a = ctx.x.auto
    assert a["roles"]["GND"] == "plane" and a["roles"]["+3V3"] == "power" and "/SIG" not in a["roles"]
    # (the bundle is physical: the pair runs in it too, but keeps its 'pair' role)
    assert [d["nets"] for d in a["buses"]] == [["/D0", "/D1", "/D2", "/D3", "/USB_DP", "/USB_DN"]]
    assert a["widths"]["+3V3"] == 0.4 and "layer_dirs" in a                  # keys of other stages are kept
    h = ctx.history["classify"]
    assert h["nets"]["/SIG"]["clearance"] == 0.4 and h["nets"]["GND"]["role"] == "plane"
    assert ctx.oracle.clearance("/SIG") == 0.4 and ctx.oracle.clearance("/D0") == 0.2    # plan clearance reaches the oracle
    # x = None works (nothing to report to)
    ctx2 = Ctx(board=b, cfg=cfg)
    C.run(ctx2, Budget(10))
    assert len(ctx2.conns) == len(conns)
    # same twice
    ctx3 = Ctx(board=b, cfg=cfg, x=X())
    C.run(ctx3, Budget(10_000))
    assert ctx3.conns == conns and ctx3.x.auto == a


def test_roles_argument_overrides_the_table():
    b = _width_board(0.4, 0.2)
    cfg = plan.resolve(b, None)
    cs = C.connections(b, cfg, None, {"/S": "power", "+5V": "signal"})
    by = {c.net: c for c in cs}
    assert by["/S"].role == "power" and by["/S"].width == 0.4 and by["+5V"].role == "signal" and by["+5V"].width == 0.2


# ── corpus ────────────────────────────────────────────────────────────────
def corpus_board(name: str):
    """model.Board of an (unrouted) benchmark board, from a cached extract; None when it cannot be made."""
    f = WORK / f"extract_{name}.json"
    if not f.exists():
        try:
            from copper_kicad.pcb.guided import session
            d = WORK / "boards" / name
            d.mkdir(parents=True, exist_ok=True)
            for src in (common.CORPUS / name).iterdir():
                if src.is_file() and src.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
                    shutil.copy2(src, d / src.name)
            ex = session._run(d / f"{name}.kicad_pcb", [{"op": "extract"}], save=False)[-1]
            f.write_text(json.dumps(ex), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"    (no extract for {name}: {type(e).__name__}: {str(e)[:120]})")
            return None
    ex = json.loads(f.read_text(encoding="utf-8"))
    return model.Board(ex, model.dru_minimums(common.CORPUS / name / f"{name}.kicad_dru"))


def test_corpus_connections():
    done = 0
    total = 0
    spent = 0.0
    for name in CORPUS:
        b = corpus_board(name)
        if b is None:
            continue
        cfg = plan.resolve(b, None)
        t0 = time.perf_counter()
        ctx = Ctx(board=b, cfg=cfg, x=X())
        rep = C.run(ctx, Budget(10 ** 9))
        dt = time.perf_counter() - t0
        spent += dt
        cs = ctx.conns
        total += len(cs)
        want = sum(len(cl) - 1 for n, cl in b.clusters.items() if len(cl) > 1)
        left = rep.data["skipped"]          # only the promised default: a ground net without a pour, for pcb_pour
        assert len(cs) + sum(s["connections"] for s in left) == want, (name, len(cs), want)   # nothing lost
        assert all("pcb_pour" in s["reason"] and not b.nets[s["net"]].has_pour for s in left), (name, left)
        assert [c.idx for c in cs] == list(range(len(cs)))
        assert len({c.rank for c in cs}) == len(cs)                                        # a total order
        for c in cs:
            assert c.width >= c.neck >= b.rules["track_width"] - 1e-9 and c.clearance >= b.rules["clearance"] - 1e-9
            assert c.role in ROLE_ORDER and c.a.pad and c.est >= 0
            assert (c.b.pad is None) == (c.role == "plane" and b.nets[c.net].has_pour) or c.role != "plane"
        ctx2 = Ctx(board=b, cfg=cfg, x=X())
        C.run(ctx2, Budget(10 ** 9))
        assert ctx2.conns == cs and ctx2.x.auto == ctx.x.auto, name                        # same twice
        # no power net is ever narrower than its class, and never wider than its smallest pad
        for n, t in ctx.history["classify"]["nets"].items():
            if t["role"] == "power" and t["width"] > t["neck"] + 1e-9:
                minor = min(m for m in (C.pad_minor(p) for p in b.nets[n].pads) if m > 0)
                assert t["width"] <= max(0.8 * minor, b.nets[n].width) + 1e-9, (name, n)
        print(f"    {name:20s} {len(cs):4d} conns  {dt * 1e3:6.1f} ms  {rep.notes[0].split(': ')[1]}")
        done += 1
    if not done:
        return "skipped: no corpus extract available"
    print(f"    classify.run: {total} connections on {done} boards in {spent * 1e3:.0f} ms "
          f"({total / max(spent, 1e-9):,.0f} connections/s)")


def test_corpus_known_facts():
    b = corpus_board("coldfire")
    if b is None:
        return "skipped: no corpus extract available"
    cfg = plan.resolve(b, None)
    t = C.net_table(b, cfg, None)
    assert t["GND"]["role"] == "plane" and t["+3.3V"]["role"] == "plane"
    assert t["GNDA"]["role"] == "power" and t["GNDA"]["skip"] and t["GNDA"]["analog"]   # left for a pour by default
    assert not any(c.net == "GNDA" for c in C.connections(b, cfg, None, {}))
    cfg = plan.resolve(b, {"nets": {"GNDA": {"skip": False}}})                              # ... routed when the plan says so
    assert C.net_table(b, cfg, None)["GNDA"]["unskipped"]
    cs = C.connections(b, cfg, None, {})
    assert any(c.net == "GNDA" for c in cs)
    sb = corpus_board("synth_bus_cross")
    if sb is not None:
        bl = C.detect_buses(sb)
        assert len(bl) == 1 and len(bl[0]["nets"]) == 24 and bl[0]["pad_order"] == "reversed"


# ── runner ────────────────────────────────────────────────────────────────
def main(argv):
    names = [a for a in argv if not a.startswith("-")]
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f) and (not names or n in names)]
    failed = 0
    for n, f in tests:
        t0 = time.perf_counter()
        try:
            r = f()
            print(f"ok   {n}  ({time.perf_counter() - t0:.2f} s){'  ' + r if isinstance(r, str) else ''}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
