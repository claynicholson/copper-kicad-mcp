"""Router v2 plan fields (design §6.1): every new field is accepted and resolved, bad values and unknown
fields raise BAD_PLAN with a fix, defaults are identical to no plan, and the v1 part of the plan is untouched.

Run:  python tests/test_v2_planx.py      (pure Python, no KiCad)
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.pcb import guided  # noqa: E402
from copper_kicad.pcb.guided import model, plan as P  # noqa: E402
from copper_kicad.pcb.v2 import planx as X  # noqa: E402
from copper_kicad.pcb.v2.types import ROLES  # noqa: E402


# ── a small synthetic board (no KiCad) ────────────────────────────────────
def board(layers=("F.Cu", "B.Cu"), pour=True):
    """U1: 2 x 6 pads at 1.0 mm pitch; J1: 6 pads in a row; C1, Y1: two pads each. 30 x 20 mm."""
    pads = []

    def pad(ref, num, net, x, y, hw=0.3, hh=0.5, kind="smd"):
        pads.append((ref, str(num), net, x, y, hw, hh, kind))
    top = ["/D0", "/D1", "/D2", "/D3", "/USB_DP", "/USB_DN"]
    bot = ["GND", "+3V3", "/XTAL1", "/XTAL2", "/A0", "VBUS"]
    for i, n in enumerate(top):
        pad("U1", i + 1, n, 10.0 + i, 8.0)
    for i, n in enumerate(bot):
        pad("U1", 12 - i, n, 10.0 + i, 12.0)
    for i, n in enumerate(["/D0", "/D1", "/D2", "/D3", "/USB_DP", "/USB_DN"]):
        pad("J1", i + 1, n, 22.0, 4.0 + 2.0 * i, 0.5, 0.5, "tht")
    pad("C1", 1, "+3V3", 6.0, 12.0, 0.4, 0.45)
    pad("C1", 2, "GND", 6.0, 14.0, 0.4, 0.45)
    pad("Y1", 1, "/XTAL1", 12.0, 16.0)
    pad("Y1", 2, "/XTAL2", 14.0, 16.0)
    pad("R1", 1, "/A0", 16.0, 16.0)
    pad("R1", 2, "VBUS", 18.0, 16.0)
    pad("R2", 1, "VBUS", 26.0, 16.0)
    pad("R2", 2, "/A0", 26.0, 14.0)
    names = sorted({p[2] for p in pads})
    ex = {"layers": list(layers),
          "rules": {"clearance": 0.15, "track_width": 0.15, "via": 0.6, "drill": 0.3, "edge_clearance": 0.3,
                    "hole_clearance": 0.25, "hole_to_hole": 0.25, "annular": 0.1,
                    "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.2, "dp_width": 0.2}},
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.2, "dp_width": 0.2} for i, n in enumerate(names)},
          "outline": {"closed": True, "polys": [{"o": [[0, 0], [30, 0], [30, 20], [0, 20]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [], "vias": [], "zones": [], "mask_openings": [],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    allL = list(range(len(layers)))
    for i, (ref, num, net, x, y, hw, hh, kind) in enumerate(pads):
        lays = allL if kind == "tht" else [0]
        d = {"ref": ref, "num": num, "net": net, "x": x, "y": y, "kind": kind, "layers": lays, "c": i + 1,
             "shapes": [[lays, "box", [x, y, 1.0, 0.0, hw, hh, 0.0]]]}
        if kind == "tht":
            d["hole"] = [x, y, 1.0, 0.0, 0.25, 0.25]
        ex["pads"].append(d)
    if pour:
        ex["zones"].append({"name": "gnd", "net": "GND", "layers": [len(layers) - 1], "filled": True,
                            "polys": [{"o": [[0.5, 0.5], [29.5, 0.5], [29.5, 19.5], [0.5, 19.5]]}], "fills": {}})
    return model.Board(ex)


def bad(b, plan, *needles, fix=None):
    try:
        P.resolve(b, plan)
    except ToolError as e:
        assert e.code == "BAD_PLAN", e.code
        text = str(e)
        for n in needles:
            assert n in text, (n, text)
        if fix is not None:
            assert fix in (e.fix or ""), (fix, e.fix)
        return e
    raise AssertionError(f"plan accepted: {plan}")


# ── schema ────────────────────────────────────────────────────────────────
def test_schema_is_extended_once_and_documents_every_field():
    for k in X.V2_GLOBAL:
        assert k in P.GLOBAL_FIELDS and P.GLOBAL_FIELDS[k], k
    for k in X.V2_NET:
        assert k in P.NET_FIELDS, k
    for k in X.V2_REGION:
        assert k in P.REGION_FIELDS, k
    for k in X.V2_TOP:
        assert k in P.PLAN_FIELDS, k
    assert set(X.V2_GLOBAL) == {"engine", "stages", "tile_mm", "negotiation_iters", "power_width_factor", "fanout", "polish", "plane_rescue",
                                "via_frugal", "constraints"}
    assert set(X.V2_NET) == {"role", "protect", "via_budget", "waypoints", "between", "side_of", "tree", "widths", "z"}
    assert set(X.V2_TOP) == {"buses", "fanout", "reserve", "groups"} and set(X.V2_REGION) == {"capacity_scale", "no_vias"}
    # electrical constraints (design section 14.3): the pair extras join v1's pair table, which validates diff_pairs
    assert set(X.PAIR_X_FIELDS) == {"skew", "uncoupled", "coupled", "z"} and set(X.PAIR_X_FIELDS) < set(P.PAIR_FIELDS)
    assert set(X.GROUP_FIELDS) == {"nets", "tol", "len", "amp", "name", "off"}
    assert len(X.SCHEMA_V2.encode()) <= 819 + 260                                 # one line for the constraint fields
    # no v2 name shadows a v1 field with another meaning: the v1 help texts are still the v1 ones
    assert "track width mm" in P.NET_FIELDS["width"] and "max vias per connection" in P.NET_FIELDS["max_vias"]
    text = P.SCHEMA_TEXT
    for k in list(X.V2_GLOBAL) + list(X.V2_NET) + list(X.V2_TOP) + list(X.V2_REGION):
        assert k in text, k
    assert guided.SCHEMA_TEXT == text and guided.PLAN_FIELDS is P.PLAN_FIELDS       # what the tools show
    n = len(text)
    X.extend_schema(P)                                                             # idempotent
    assert len(P.SCHEMA_TEXT) == n and text.count("// v2 fields") == 1
    assert len(text) < 2000, len(text)                                             # stays cheap to send


def test_defaults_are_identical_to_no_plan():
    b = board()
    base = P.resolve(b, None)
    x0 = base.x
    assert isinstance(x0, X.V2Cfg) and base.engine == "v2" == x0.engine == X.DEFAULT_ENGINE
    assert x0.stages == X.STAGES == base.stages and x0.last_stage == "judge" and x0.has("detail")
    assert (x0.tile_mm, x0.negotiation_iters, x0.power_width_factor, x0.fanout, x0.polish) == (None, 30, 2.0, "auto", True)
    assert x0.nets == {} and x0.buses == [] and x0.fanout_parts == {} and x0.reserve == [] and x0.regions == []
    assert x0.given == [] and x0.warnings == []
    assert x0.auto == {"roles": {}, "buses": [], "widths": {}, "layer_dirs": {}}
    nx = x0.net("/D0")
    assert (nx.role, nx.protect, nx.via_budget, nx.waypoints, nx.between, nx.side_of, nx.tree) == (None, False, None, (), (), (), None)
    assert x0.bus_of("/D0") is None and x0.fanout_for("U1") == {"style": "auto", "side": None, "layer": None}
    assert x0.capacity_scale(10, 10) == 1.0 and x0.no_vias(10, 10) is False
    for plan in ({}, {"global": {}}, {"nets": {}}, {"regions": [], "buses": [], "reserve": [], "fanout": {}},
                 {"global": {"engine": "v2"}}):
        c = P.resolve(b, plan)
        assert c.x.as_dict() == x0.as_dict(), plan
        assert c.used == base.used or plan == {"global": {"engine": "v2"}}, plan
        assert c.warnings == base.warnings
    # the classic router stays selectable, and selecting it changes nothing but the engine
    g = P.resolve(b, {"global": {"engine": "guided"}})
    assert g.engine == "guided" == g.x.engine and g.warnings == base.warnings
    assert {**g.x.as_dict(), "engine": "v2"} == x0.as_dict()
    # the v1 configuration of every net is the same object-for-object data with and without v2 fields
    v2 = P.resolve(b, {"global": {"engine": "v2", "tile_mm": 0.5}, "nets": {"/D0": {"role": "bus", "via_budget": 1}}})
    for n in base.nets:
        for k in P.NetCfg.__slots__:
            if k == "explicit":              # "named in the plan" is true for /D0 now; a v1 fact, not a routing change
                continue
            assert getattr(base.nets[n], k) == getattr(v2.nets[n], k), (n, k)
    assert (base.step, base.via_cost, base.bend_cost, base.time_budget) == (v2.step, v2.via_cost, v2.bend_cost, v2.time_budget)
    # and default_plan / the suggested plan carries no v2 field (it runs under either engine)
    dp = P.default_plan(b)
    assert not set(dp.get("global", {})) & set(X.V2_GLOBAL) and not set(dp) & set(X.V2_TOP)


def test_every_new_field_is_accepted_and_resolved():
    b = board()
    plan = {
        "global": {"engine": "v2", "stages": ["global", "classify", "power"], "tile_mm": 0.5, "negotiation_iters": 12,
                   "power_width_factor": 3, "fanout": "none", "polish": False},
        "nets": {
            "/D?": {"role": "bus", "via_budget": 1},
            "/D0": {"via_budget": 2, "protect": True, "tree": "chain",
                    "waypoints": [[15, 5], [18.5, 4.0, "F.Cu"], [20, 4, "bottom"]],
                    "between": [["U1.1", "U1.2"]], "side_of": [{"ref": "C1", "side": "ccw"}]},
            "+3V3": {"role": "power", "tree": "star"},
            "GND": {"role": "plane"},
            "/USB_DP": {"role": "pair"},
            "/XTAL1": {"role": "critical", "between": ["Y1.1", "Y1.2"], "side_of": {"ref": "Y1", "side": "cw"}},
        },
        "buses": [{"nets": ["/D?"], "order": ["/D3", "/D2", "/D1", "/D0"], "layer": "F.Cu", "spacing": 0.45},
                  {"nets": ["/XTAL1", "/XTAL2"]}],
        "fanout": {"U1": {"style": "via", "side": "north", "layer": "B.Cu"}, "R?": {"style": "out"}},
        "reserve": [{"at": [8.0, 13.0], "for": "GND"}],
        "regions": [{"rect": [9, 9, 16, 11], "capacity_scale": 0.5, "no_vias": True, "name": "under U1"},
                    {"rect": [0, 0, 5, 5], "cost": 3, "capacity_scale": 2, "layers": ["B.Cu"], "nets": ["/D*"]},
                    {"rect": [20, 0, 30, 3], "forbid": True}],
    }
    snapshot = copy.deepcopy(plan)
    cfg = P.resolve(b, plan)
    assert plan == snapshot                                                   # the caller's plan is not modified
    x = cfg.x
    assert cfg.engine == x.engine == "v2" and cfg.stages == x.stages == ("classify", "power", "global")
    assert x.last_stage == "global" and not x.has("detail")
    assert (x.tile_mm, x.negotiation_iters, x.power_width_factor, x.fanout, x.polish) == (0.5, 12, 3.0, "none", False)
    d0 = x.net("/D0")
    assert d0.role == "bus" and d0.via_budget == 2 and d0.protect and d0.tree == "chain"      # exact beats the glob
    assert d0.waypoints == ((15.0, 5.0, None), (18.5, 4.0, 0), (20.0, 4.0, 1))
    assert d0.between == (("U1.1", "U1.2"),) and d0.side_of == (("C1", "ccw"),)
    assert x.net("/D2").via_budget == 1 and x.net("/D2").role == "bus" and not x.net("/D2").protect
    assert x.net("+3V3").role == "power" and x.net("+3V3").tree == "star" and x.net("GND").role == "plane"
    assert x.net("/USB_DP").role == "pair" and x.net("/USB_DN").role is None
    assert x.net("/XTAL1").between == (("Y1.1", "Y1.2"),) and x.net("/XTAL1").side_of == (("Y1", "cw"),)
    assert x.buses[0] == {"nets": ["/D0", "/D1", "/D2", "/D3"], "order": ["/D3", "/D2", "/D1", "/D0"], "layer": 0,
                          "spacing": 0.45}
    assert x.buses[1] == {"nets": ["/XTAL1", "/XTAL2"], "order": ["/XTAL1", "/XTAL2"], "layer": None, "spacing": None}
    assert x.bus_of("/D1") is x.buses[0] and x.bus_of("/XTAL2") is x.buses[1] and x.bus_of("/A0") is None
    assert x.fanout_parts == {"U1": {"style": "via", "side": "n", "layer": 1},
                              "R1": {"style": "out", "side": None, "layer": None},
                              "R2": {"style": "out", "side": None, "layer": None}}
    assert x.fanout_for("J1")["style"] == "none"                               # global.fanout = none, no override
    assert x.reserve == [{"at": (8.0, 13.0), "net": "GND"}]
    assert [(r.idx, r.capacity_scale, r.no_vias) for r in x.regions] == [(0, 0.5, True), (1, 2.0, False)]
    assert x.capacity_scale(10, 10) == 0.5 and x.capacity_scale(10, 10, layer=1) == 0.5 and x.capacity_scale(20, 10) == 1.0
    assert x.capacity_scale(2, 2, layer=1) == 2.0 and x.capacity_scale(2, 2, layer=0) == 1.0
    assert x.capacity_scale(2, 2, layer=1, net="/D1") == 2.0 and x.capacity_scale(2, 2, layer=1, net="GND") == 1.0
    assert x.no_vias(10, 10) and not x.no_vias(2, 2) and x.no_vias(12, 9.5, net="/D0")
    # the v1 side of the same regions is unchanged: no cost, no forbid on the first one
    assert (cfg.regions[0].cost, cfg.regions[0].forbid) == (1.0, False) and cfg.regions[1].cost == 3.0 and cfg.regions[2].forbid
    # plan_used echoes what was applied
    u = cfg.used
    assert u["global"]["engine"] == "v2" and u["global"]["tile_mm"] == 0.5 and u["nets"]["/D0"]["via_budget"] == 2
    assert u["buses"][0] == {"nets": ["/D3", "/D2", "/D1", "/D0"], "layer": "F.Cu", "spacing": 0.45}
    assert u["fanout"]["U1"]["style"] == "via" and u["reserve"] == [{"at": [8.0, 13.0], "for": "GND"}]
    assert u["regions"][0]["capacity_scale"] == 0.5
    assert x.given == sorted(x.given) and "global.tile_mm" in x.given and "nets.waypoints" in x.given and "buses" in x.given
    assert not any("ignores" in w for w in cfg.warnings)
    # same plan, same resolution
    assert P.resolve(b, copy.deepcopy(plan)).x.as_dict() == x.as_dict()
    for r in ROLES:
        if r not in ("pair", "plane"):
            assert P.resolve(b, {"nets": {"/A0": {"role": r}}}).x.net("/A0").role == r


def test_v2_fields_under_the_guided_engine_are_validated_and_flagged():
    b = board()
    cfg = P.resolve(b, {"nets": {"/D0": {"via_budget": 2}}, "global": {"tile_mm": 1, "engine": "guided"}})
    assert cfg.engine == "guided" and cfg.x.net("/D0").via_budget == 2
    w = [m for m in cfg.warnings if "v2-only" in m]
    assert len(w) == 1 and "nets.via_budget" in w[0] and "global.tile_mm" in w[0] and "engine='guided'" in w[0]
    # under the default engine the same fields are simply used: no such note
    dflt = P.resolve(b, {"nets": {"/D0": {"via_budget": 2}}, "global": {"tile_mm": 1}})
    assert dflt.engine == "v2" and dflt.x.net("/D0").via_budget == 2 and not [m for m in dflt.warnings if "v2-only" in m]
    bad(b, {"nets": {"/D0": {"via_budget": -1}}}, "via_budget", "out of range")     # still validated
    # via_budget above max_vias is clamped, with a note
    c = P.resolve(b, {"global": {"engine": "v2"}, "nets": {"/D0": {"via_budget": 5, "max_vias": 2}}})
    assert c.x.net("/D0").via_budget == 2 and any("above max_vias" in m for m in c.warnings)
    # stages without classify: classify runs anyway
    c = P.resolve(b, {"global": {"engine": "v2", "stages": ["global"]}})
    assert c.stages == ("classify", "global") and any("classify" in m for m in c.warnings)


def test_unknown_fields_raise_bad_plan_with_a_fix():
    b = board()
    bad(b, {"global": {"tile": 1}}, "unknown field", "plan.global", fix="'tile' → 'tile_mm'")
    bad(b, {"nets": {"/D0": {"via_budgett": 1}}}, "unknown field", fix="'via_budgett' → 'via_budget'")
    bad(b, {"busses": []}, "unknown field", fix="'busses' → 'buses'")
    bad(b, {"regions": [{"rect": [0, 0, 1, 1], "capacity": 2}]}, "unknown field", "regions[0]")
    bad(b, {"buses": [{"nets": ["/D?"], "pitch": 1}]}, "unknown field", "buses[0]")
    bad(b, {"fanout": {"U1": {"styl": "via"}}}, "unknown field", fix="'styl' → 'style'")
    bad(b, {"reserve": [{"at": [8, 13], "net": "GND"}]}, "unknown field", "reserve[0]")
    bad(b, {"regions": [{"rect": [0, 0, 1, 1]}]}, "needs either cost", "capacity_scale")       # still needs something


def test_bad_values_raise_bad_plan_with_the_valid_choices():
    b = board()
    G = lambda **k: {"global": k}                                             # noqa: E731
    N = lambda **k: {"nets": {"/D0": k}}                                      # noqa: E731
    bad(b, G(engine="v3"), "engine", "guided")
    bad(b, G(stages="global"), "stages")
    bad(b, G(stages=[]), "stages")
    bad(b, G(stages=["global", "detial"]), "unknown stage", fix="'detial' → 'detail'")
    bad(b, G(tile_mm=0.1), "tile_mm", "out of range")
    bad(b, G(tile_mm="1"), "tile_mm", "number")
    bad(b, G(negotiation_iters=0), "negotiation_iters")
    bad(b, G(power_width_factor=0.5), "power_width_factor")
    bad(b, G(fanout="via"), "global.fanout", fix="plan.fanout")
    bad(b, G(polish="yes"), "polish")
    bad(b, N(role="clock"), "role", "critical")
    bad(b, N(role="pair"), "no differential pair", fix="diff_pairs")
    bad(b, {"nets": {"+3V3": {"role": "plane"}}}, "no pour", fix="pcb_pour")
    bad(b, N(protect=1), "protect")
    bad(b, N(via_budget=True), "via_budget")
    bad(b, N(tree="steiner"), "tree", "mst")
    bad(b, N(waypoints=[[1, 2, 3, 4]]), "waypoints[0]")
    bad(b, N(waypoints=[]), "waypoints")
    bad(b, N(waypoints=[[40, 5]]), "not on the board", fix="the board spans")
    bad(b, N(waypoints=[[5, 5, "In1.Cu"]]), "not a copper layer")
    bad(b, {"nets": {"/D0": {"layers": ["F.Cu"], "waypoints": [[5, 5, "B.Cu"]]}}}, "not allowed", fix="F.Cu")
    bad(b, N(between=[["U1.1", "U1.99"]]), "no pad 'U1.99'", fix="U1.9")
    bad(b, N(between=[["U1.1", "U1.1"]]), "same pad twice")
    bad(b, N(between=[["U1.1"]]), "two pad names")
    bad(b, N(side_of=[{"ref": "U9", "side": "cw"}]), "no part 'U9'", fix="U1")
    bad(b, N(side_of=[{"ref": "U1", "side": "left"}]), "'cw' or 'ccw'")
    bad(b, N(side_of=[{"ref": "U1"}]), "{ref, side}")
    bad(b, {"buses": {"nets": ["/D?"]}}, "plan.buses must be a list")
    bad(b, {"buses": [{"nets": ["/D0"]}]}, "at least two")
    bad(b, {"buses": [{"order": ["/D0", "/D1"]}]}, "needs nets")
    bad(b, {"buses": [{"nets": ["/D?"], "order": ["/D0", "/D1", "/D2"]}]}, "exactly the bus nets", "/D3")
    bad(b, {"buses": [{"nets": ["/D0", "/D1"], "order": ["/D0", "/D1", "/A0"]}]}, "not in nets")
    bad(b, {"buses": [{"nets": ["/D0", "/D1"]}, {"nets": ["/D1", "/D2"]}]}, "already in buses[0]")
    bad(b, {"buses": [{"nets": ["/D0", "/NOPE"]}]}, "not on the board")
    bad(b, {"buses": [{"nets": ["/D?"], "layer": "In1.Cu"}]}, "not a copper layer")
    bad(b, {"buses": [{"nets": ["/D?"], "spacing": 0}]}, "spacing")
    bad(b, {"fanout": "none"}, "plan.fanout must be an object", fix="global.fanout")
    bad(b, {"fanout": {"U7": {"style": "via"}}}, "no part 'U7'", fix="U1")
    bad(b, {"fanout": {"U1": {"style": "dogbone"}}}, "style", "via")
    bad(b, {"fanout": {"U1": {"side": "top"}}}, "side", fix="smaller y")
    bad(b, {"reserve": [{"at": [8, 13]}]}, "needs for")
    bad(b, {"reserve": [{"at": [8], "for": "GND"}]}, "needs at")
    bad(b, {"reserve": [{"at": [80, 13], "for": "GND"}]}, "not on the board")
    bad(b, {"reserve": [{"at": [8, 13], "for": "/D?"}]}, "exactly one net")
    bad(b, {"regions": [{"rect": [0, 0, 1, 1], "capacity_scale": 0}]}, "capacity_scale", "out of range")
    bad(b, {"regions": [{"rect": [0, 0, 1, 1], "no_vias": "yes"}]}, "no_vias")
    # v1 errors are unchanged
    bad(b, {"global": {"via_cost": -1}}, "via_cost")
    bad(b, {"nets": {"/NOPE": {"width": 0.3}}}, "not on the board")


def test_merge_patch_and_apply_clearances():
    b = board()
    plan = {"order": ["/D0", "*"], "global": {"engine": "v2"}, "nets": {"/D0": {"width": 0.3}},
            "regions": [{"rect": [0, 0, 1, 1], "cost": 2}]}
    keep = copy.deepcopy(plan)
    m = X.merge_patch(plan, {"nets": {"/D0": {"via_budget": 3}, "/D1": {"priority": 90}}})
    assert plan == keep and m["nets"] == {"/D0": {"width": 0.3, "via_budget": 3}, "/D1": {"priority": 90}}
    m = X.merge_patch(m, {"regions": [{"rect": [5, 5, 7, 7], "capacity_scale": 0.6}], "order": ["/D1", "/D0", "*"]})
    assert len(m["regions"]) == 2 and m["order"] == ["/D1", "/D0", "*"] and m["global"] == {"engine": "v2"}
    m2 = X.merge_patch(m, {"regions": [{"rect": [5, 5, 7, 7], "capacity_scale": 0.6}]})          # same patch twice
    assert len(m2["regions"]) == 2
    assert X.merge_patch(None, {"global": {"time_budget_s": 180}}) == {"global": {"time_budget_s": 180}}
    cfg = P.resolve(b, m)                                                      # the merged plan is a valid plan
    assert cfg.x.net("/D0").via_budget == 3 and cfg.x.regions[0].capacity_scale == 0.6
    try:
        X.merge_patch(plan, {"move": {"ref": "C1", "by": [0.4, 0]}})
    except ValueError:
        pass
    else:
        raise AssertionError("a placement patch is not a plan fragment")

    class FakeOracle:
        def __init__(self):
            self.set = {}

        def set_clearance(self, net, mm):
            self.set[net] = mm
    o = FakeOracle()
    cfg = P.resolve(b, {"nets": {"/D0": {"clearance": 0.3}, "/D1": {"clearance": 0.1}}})
    assert X.apply_clearances(o, b, cfg) == 1 and o.set == {"/D0": 0.3}


def test_auto_preview_is_compact_and_follows_the_plan():
    b = board()
    cfg = P.resolve(b, None)
    a = X.auto_preview(b, cfg)
    assert set(a) == {"roles", "buses", "widths", "layer_dirs", "source"}
    if a["source"] == "rules":                                                 # classify not built yet: the §4.3 rules
        assert a["roles"]["GND"] == "plane" and a["roles"]["+3V3"] == "power" and a["roles"]["VBUS"] == "power"
        assert a["roles"]["/USB_DP"] == "pair" and a["roles"]["/USB_DN"] == "pair"
        assert a["roles"]["/XTAL1"] == "critical" and "/D0" not in a["roles"]                # signal is the default
        # power width: min(class x 2, 0.8 x smallest pad minor dimension, 1.0); U1 pads are 0.6 wide -> 0.4
        assert a["widths"] == {"+3V3": 0.4, "VBUS": 0.4}, a["widths"]
        c2 = P.resolve(b, {"global": {"power_width_factor": 1.5}, "nets": {"VBUS": {"width": 0.25}, "/A0": {"role": "critical"}}})
        a2 = X.auto_preview(b, c2)
        assert a2["widths"] == {"+3V3": 0.3} and a2["roles"]["/A0"] == "critical"
        nb = board(pour=False)
        a3 = X.auto_preview(nb, P.resolve(nb, None))
        assert a3["roles"]["GND"] == "power"                                    # a pourless ground is routed as power
    assert X.auto_preview(b, P.resolve(b, None)) == a                           # same twice
    assert X.pad_minor(b.nets["+3V3"].pads[0]) > 0


def test_plane_rescue_field_and_the_plan_v2_takes_from_the_suggested_one():
    b = board()
    assert P.resolve(b, None).x.plane_rescue is True
    assert P.resolve(b, {"global": {"engine": "v2", "plane_rescue": False}}).x.plane_rescue is False
    bad(b, {"global": {"plane_rescue": "no"}}, "plane_rescue")
    assert P.resolve(b, None).x.via_frugal is True
    assert P.resolve(b, {"global": {"engine": "v2", "via_frugal": False}}).x.via_frugal is False
    bad(b, {"global": {"via_frugal": 1}}, "via_frugal")
    # v2_plan: engine named, the automatic ground skip turned into an explicit "route it" (skip: false; with no
    # entry both engines leave such a net for the pour), everything else (and the input) untouched
    sug = {"global": {"grid": 0.2, "via_cost": 5},
           "nets": {"GNDA": {"skip": True}, "GNDB": {"skip": True, "priority": 20}, "/CLK": {"priority": 85},
                    "/X": {"skip": False}}}
    keep = copy.deepcopy(sug)
    out = X.v2_plan(sug)
    assert sug == keep
    assert out["global"] == {"grid": 0.2, "via_cost": 5, "engine": "v2"}
    assert out["nets"] == {"GNDA": {"skip": False}, "GNDB": {"skip": False, "priority": 20}, "/CLK": {"priority": 85},
                           "/X": {"skip": False}}
    assert X.v2_plan(None) == {"global": {"engine": "v2"}}
    assert X.v2_plan(out) == out                                                    # idempotent


def test_no_clock_no_random():
    src = Path(X.__file__).read_text(encoding="utf-8")
    for banned in ("import time", "from time", "import random", "datetime", "perf_counter", "monotonic"):
        assert banned not in src, banned
    assert not getattr(X, "STUB", False)


# ── electrical constraints (design §14.3) ─────────────────────────────────
def test_constraint_fields_are_accepted_and_resolved():
    b = board()
    plan = {
        "global": {"constraints": "report"},
        "groups": [{"nets": ["/D?"], "tol": 0.5, "len": 42.0, "amp": 2.0, "name": "data"},
                   {"nets": ["/A0", "/XTAL1"], "tol": 1},
                   {"nets": ["/XTAL2"], "off": True}],
        "diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "gap": 0.18, "width": 0.22, "skew": 0.15, "uncoupled": 5,
                        "coupled": False, "z": 90}],
        "nets": {"/A0": {"widths": {"F.Cu": 0.15, "bottom": 0.3}, "z": 50, "width": 0.25}, "/D1": {"z": 55}},
    }
    snapshot = copy.deepcopy(plan)
    cfg = P.resolve(b, plan)
    assert plan == snapshot
    cp = cfg.x.cons_plan
    assert cp["mode"] == "report" and cp["pairs_given"] is True and cp["no_groups"] is False
    assert cp["groups"] == [{"name": "data", "nets": ("/D0", "/D1", "/D2", "/D3"), "tol": 0.5, "len": 42.0, "amp": 2.0},
                            {"name": None, "nets": ("/A0", "/XTAL1"), "tol": 1.0, "len": None, "amp": None}]
    assert cp["off"] == ("/XTAL2",)
    assert cp["pairs"] == {"/USB_DP": {"n": "/USB_DN", "gap": 0.18, "width": 0.22, "skew": 0.15, "uncoupled": 5.0,
                                       "coupled": False, "z": 90.0}}
    assert cp["widths"] == {"/A0": ((0, 0.15), (1, 0.3))} and cp["z"] == {"/A0": 50.0, "/D1": 55.0}
    assert cfg.x.cons is None                                    # resolved by the engine (it needs the project's rules)
    # the v1 side: the pair is still v1's pair (gap, width), `width` stays the net's width for unlisted layers
    assert cfg.nets["/USB_DP"].dp_partner == "/USB_DN" and cfg.nets["/USB_DP"].dp_gap == 0.18
    assert cfg.nets["/USB_DP"].width == 0.22 and cfg.nets["/A0"].width == 0.25
    # plan_used echoes what was applied; `given` names every constraint field the plan set
    u = cfg.used
    assert u["diff_pairs"] == [{"p": "/USB_DP", "n": "/USB_DN", "gap": 0.18, "width": 0.22, "skew": 0.15,
                                "uncoupled": 5.0, "coupled": False, "z": 90.0}]
    assert u["groups"][0] == {"name": "data", "nets": ["/D0", "/D1", "/D2", "/D3"], "tol": 0.5, "len": 42.0, "amp": 2.0}
    assert u["groups"][2] == {"nets": ["/XTAL2"], "off": True} and u["global"]["constraints"] == "report"
    for k in ("global.constraints", "groups", "nets.widths", "nets.z", "diff_pairs.skew", "diff_pairs.uncoupled",
              "diff_pairs.coupled", "diff_pairs.z"):
        assert k in cfg.x.given, (k, cfg.x.given)
    d = cfg.x.as_dict()["constraints"]
    assert d["mode"] == "report" and d["groups"][0]["nets"] == ["/D0", "/D1", "/D2", "/D3"] and d["off"] == ["/XTAL2"]
    assert d["pairs"] == {"/USB_DP": {"n": "/USB_DN", "gap": 0.18, "width": 0.22, "skew": 0.15, "uncoupled": 5.0,
                                      "coupled": False, "z": 90.0}}
    assert P.resolve(b, copy.deepcopy(plan)).x.as_dict() == cfg.x.as_dict()
    # the cheapest useful plan; one net is a group with len; groups: [] = no length groups at all
    c1 = P.resolve(b, {"groups": [{"nets": ["/D*"], "tol": 0.5}]})
    assert c1.x.cons_plan["groups"][0]["nets"] == ("/D0", "/D1", "/D2", "/D3") and c1.x.cons_plan["mode"] == "auto"
    assert c1.x.cons_plan["pairs_given"] is False and c1.x.cons_plan["pairs"] == {}
    assert P.resolve(b, {"groups": [{"nets": ["/A0"], "tol": 0.2, "len": 30}]}).x.cons_plan["groups"][0]["len"] == 30.0
    c0 = P.resolve(b, {"groups": []})
    assert c0.x.cons_plan["no_groups"] is True and c0.x.cons_plan["groups"] == [] and "groups" in c0.x.given
    for m in X.CONSTRAINT_MODES:
        assert P.resolve(b, {"global": {"constraints": m}}).x.cons_plan["mode"] == m
    # a declared group beats an `off` entry that names the same net
    c2 = P.resolve(b, {"groups": [{"nets": ["/D0", "/D1"], "tol": 1}, {"nets": ["/D?"], "off": True}]})
    assert c2.x.cons_plan["off"] == ("/D2", "/D3")
    # a plan without constraint fields resolves to the defaults, object for object
    assert P.resolve(b, None).x.cons_plan == X.cons_plan_default()
    assert P.resolve(b, {"diff_pairs": []}).x.cons_plan["pairs_given"] is True          # the veto of name detection


def test_bad_constraint_values_raise_bad_plan_with_a_fix():
    b = board()
    bad(b, {"global": {"constraints": "automatic"}}, "global.constraints", "auto", fix="did you mean 'auto'")
    bad(b, {"groups": {"nets": ["/D0"]}}, "plan.groups must be a list")
    bad(b, {"groups": [{"nets": ["/D?"], "tolerance": 0.5}]}, "unknown field", "tolerance", fix="'tolerance' → 'tol'")
    bad(b, {"groups": [{"tol": 0.5}]}, "needs nets")
    bad(b, {"groups": [{"nets": ["/D?"]}]}, "needs tol", fix="tol")
    bad(b, {"groups": [{"nets": ["/D?"], "tol": 0}]}, "groups[0].tol", "out of range")
    bad(b, {"groups": [{"nets": ["/D?"], "tol": "tight"}]}, "groups[0].tol", "must be a number")
    bad(b, {"groups": [{"nets": ["/D?"], "tol": 0.5, "len": 0}]}, "groups[0].len", "out of range")
    bad(b, {"groups": [{"nets": ["/D?"], "tol": 0.5, "amp": 0.1}]}, "groups[0].amp", "out of range")
    bad(b, {"groups": [{"nets": ["/D?"], "tol": 0.5, "name": ""}]}, "groups[0].name")
    bad(b, {"groups": [{"nets": ["/D?"], "tol": 0.5, "off": "yes"}]}, "groups[0].off")
    bad(b, {"groups": [{"nets": ["/D0"], "tol": 0.5}]}, "at least two routable nets", fix="len")
    bad(b, {"groups": [{"nets": ["/NOPE*"], "tol": 0.5}]}, "matches no net")
    # a net in two groups
    e = bad(b, {"groups": [{"nets": ["/D0", "/D1"], "tol": 0.5}, {"nets": ["/D1", "/D2"], "tol": 0.5}]},
            "/D1 is already in groups[0]", "one length group")
    assert "merge" in e.fix
    bad(b, {"diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "skew": 0}]}, "diff_pairs[0].skew", "out of range")
    bad(b, {"diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "uncoupled": -1}]}, "diff_pairs[0].uncoupled")
    bad(b, {"diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "coupled": "no"}]}, "diff_pairs[0].coupled", fix="false")
    bad(b, {"diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "z": 5}]}, "diff_pairs[0].z", "out of range")
    bad(b, {"diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "max_skew_mm": 0.1}]}, "unknown field", "max_skew_mm",
        fix="'max_skew_mm' → 'skew'")
    bad(b, {"nets": {"/A0": {"widths": 0.2}}}, "widths must be an object", fix="width")
    bad(b, {"nets": {"/A0": {"widths": {"In5.Cu": 0.2}}}}, "is not a copper layer")
    bad(b, {"nets": {"/A0": {"widths": {"F.Cu": 0.05}}}}, "below the board's minimum track width", fix="0.15")
    bad(b, {"nets": {"/A0": {"z": 1000}}}, "nets['/A0'].z", "out of range")
    bad(b, {"nets": {"/A0": {"impedance_ohm": 50}}}, "unknown field", "impedance_ohm", fix="'impedance_ohm' → 'z'")


def test_constraint_fields_under_the_guided_engine_are_validated_and_flagged():
    b = board()
    cfg = P.resolve(b, {"global": {"engine": "guided", "constraints": "auto"},
                        "groups": [{"nets": ["/D?"], "tol": 0.5}],
                        "diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "skew": 0.2}]})
    assert cfg.engine == "guided"
    w = [m for m in cfg.warnings if "ignores" in m]
    assert len(w) == 1 and "groups" in w[0] and "diff_pairs.skew" in w[0] and "global.constraints" in w[0]
    bad(b, {"global": {"engine": "guided"}, "groups": [{"nets": ["/D?"]}]}, "needs tol")      # still validated


def test_merge_patch_appends_groups():
    plan = {"groups": [{"nets": ["/D?"], "tol": 0.5}]}
    out = X.merge_patch(plan, {"groups": [{"nets": ["/A0", "/XTAL1"], "tol": 1}]})
    assert out["groups"] == [{"nets": ["/D?"], "tol": 0.5}, {"nets": ["/A0", "/XTAL1"], "tol": 1}]
    assert plan == {"groups": [{"nets": ["/D?"], "tol": 0.5}]}
    assert X.merge_patch(out, {"groups": [{"nets": ["/D?"], "tol": 0.5}]}) == out          # the same entry is not doubled
    P.resolve(board(), out)


if __name__ == "__main__":
    fails = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
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
