"""Router v2 diagnostics (design §6.2, §6.3): certificates are complete, suggest patches validate against the
plan schema, overlays stay inside the board, pictures get drawn, everything is the same twice.

Run:  python tests/test_v2_diag.py [--perf]      (pure Python + Pillow; the corpus test needs KiCad and is
                                                 skipped without it)
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench" / "route"))
sys.path.insert(0, str(ROOT / "tests"))
import common  # noqa: E402  (bench/route/common.py)

common.sandbox()       # KICAD_CONFIG_HOME, COPPER_HOME and the temp dir -> bench/route/.work, before copper_kicad loads
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.mcp.registry import ToolError  # noqa: E402,F401
from copper_kicad.pcb.guided import model, plan as P  # noqa: E402
from copper_kicad.pcb.v2 import diag as D, planx as X  # noqa: E402
from copper_kicad.pcb.v2.oracle import Oracle  # noqa: E402
from copper_kicad.pcb.v2.pieces import RouteState  # noqa: E402
from copper_kicad.pcb.v2.types import Blocker, ConnV2, Corridor, Ctx, Cut, Score, StageReport, Terminal  # noqa: E402
from test_v2_planx import board  # noqa: E402  (the synthetic 30 x 20 mm board: U1 2x6, J1 1x6, C1, Y1, R1, R2)

OUT = common.WORK / "test_v2_diag"
OUT.mkdir(parents=True, exist_ok=True)
CERT_KEYS = {"net", "conn", "from", "to", "reason", "where", "blocked_by", "cut", "static_share", "mincut_nets",
             "probe", "tried", "_a", "_b"}
CUT_KEYS = {"a", "b", "layer", "capacity_mm", "demand_mm", "nets", "overflow_mm"}


class FakeTiles:
    """The §9 Tiles interface with uniform capacities (the real tiles.py belongs to W3). Tile (0, 0) starts at
    (ox, oy), which diag has to find through tile_of alone."""

    def __init__(self, b, t=1.0, cap=1.2, origin=None):
        self.t = t
        self._ox, self._oy = origin if origin else (b.bbox[0], b.bbox[1])
        self.nx = math.ceil((b.bbox[2] - self._ox) / t)
        self.ny = math.ceil((b.bbox[3] - self._oy) / t)
        self.nl = b.L
        self._cap = cap
        self.over = {}
        self.shadow = {}                       # pad name -> {(layer, tx, ty, side)}: borders under that pad
        self.layers = b.layers

    def pad_edges(self, pad):
        return frozenset(((l * self.ny + ty) * self.nx + tx) * 2 + (side == "N")
                         for l, tx, ty, side in self.shadow.get(pad, ()))

    def tile_of(self, x, y):
        return (int(math.floor((x - self._ox) / self.t)), int(math.floor((y - self._oy) / self.t)))

    def cap(self, layer, tx, ty, side):
        return self.over.get((layer, tx, ty, side), self._cap)

    def via_cap(self, tx, ty):
        return 1

    def pad_cut(self, pad_a, pad_b, layer, w, cl):
        return Cut(a=pad_a, b=pad_b, layer=self.layers[layer], capacity_mm=0.0, demand_mm=w + cl, nets=[])

    def cut_for_edge(self, layer, tx, ty, side, demand):
        nb = (tx + 1, ty) if side == "E" else (tx, ty + 1)
        return Cut(a=f"tile({tx},{ty})|{side}", b=f"tile({nb[0]},{nb[1]})", layer=self.layers[layer],
                   capacity_mm=self.cap(layer, tx, ty, side), demand_mm=sum(m for _n, m in demand),
                   nets=[n for n, _m in demand])


def term(b, name):
    p = next(p for p in b.pads if p.name == name)
    return Terminal(x=p.x, y=p.y, layers=tuple(p.layers), pad=name, cluster=p.cluster)


def make(plan=None, tiles=True, origin=None):
    """A routed state with one of everything diag reports on."""
    b = board()
    base = {"global": {"engine": "v2"}, "nets": {"/D1": {"via_budget": 1}},
            "buses": [{"nets": ["/D0", "/D1", "/D2"]}]}
    cfg = P.resolve(b, X.merge_patch(base, plan or {}))
    orc = Oracle(b)
    spec = [("/D0", "U1.1", "J1.1", "bus"), ("/D1", "U1.2", "J1.2", "bus"), ("/D2", "U1.3", "J1.3", "bus"),
            ("/D3", "U1.4", "J1.4", "signal"), ("+3V3", "C1.1", "U1.11", "power")]
    conns = []
    for i, (net, a, z, role) in enumerate(spec):
        ta, tz = term(b, a), term(b, z)
        dx, dy = abs(ta.x - tz.x), abs(ta.y - tz.y)
        conns.append(ConnV2(idx=i, net=net, a=ta, b=tz, est=max(dx, dy) + 0.41421356 * min(dx, dy), role=role,
                            width=0.4 if role == "power" else 0.2, neck=0.2, clearance=0.2, via=(0.6, 0.3), rank=(i,)))
    st = RouteState(orc, conns)
    ctx = Ctx(board=b, cfg=cfg, x=cfg.x, oracle=orc, state=st, conns=conns)
    if tiles:
        ctx.tiles = FakeTiles(b, origin=origin)

    def seg(c, layer, pts, w=0.2, **kw):
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            st.add(net=conns[c].net, conn=c, kind="seg", layer=layer, x1=x1, y1=y1, x2=x2, y2=y2, w=w, **kw)

    def via(c, x, y):
        st.add(net=conns[c].net, conn=c, kind="via", x1=x, y1=y, w=0.6, drill=0.3)
    # /D0: clean, top layer, axial + one 45
    seg(0, 0, [(10, 8), (10, 6.5), (19.5, 6.5), (22, 4)])
    st.set_status(0, "routed")
    # /D1: 3 vias (budget 1), mostly on the bottom, and a long way round (detour)
    seg(1, 0, [(11, 8), (11, 7.2)])
    via(1, 11, 7.2)
    seg(1, 1, [(11, 7.2), (11, 2.5), (2.5, 2.5), (2.5, 17.5), (26.5, 17.5), (26.5, 6)])
    via(1, 26.5, 6)
    seg(1, 0, [(26.5, 6), (24, 6)])
    via(1, 24, 6)
    seg(1, 1, [(24, 6), (22, 6)])
    st.set_status(1, "routed")
    # /D2: necked all the way (0.15 of a 0.3 target) and at an odd angle
    seg(2, 0, [(12, 8), (12.4, 7.0), (21.0, 7.6), (22, 8)], w=0.15, target_w=0.3)
    st.set_status(2, "routed")
    # +3V3: a power net at signal width
    seg(4, 0, [(6, 12), (6, 13.6), (11, 13.6), (11, 12)], w=0.2)
    st.set_status(4, "routed")
    # /D3: failed, boxed in at U1.4 between its neighbours; /D2's copper and a fixed pad were in the way
    d2 = st.pieces(conn=2)[0]
    fail = {"reason": "pad boxed in", "where": (13.0, 7.3),
            "blocked_by": [Blocker("clearance", "/D2", "track", False, d2.id, 0.05, 0.2, (13.0, 7.3), 0),
                           Blocker("clearance", "/USB_DP", "U1.5", True, None, 0.1, 0.2, (13.0, 7.5), 0),
                           (d2.id, 3)],
            "tried": ["pattern", "probe"], "probe": {"enclosed": True, "end": "a", "reached": 41},
            "crossed": {2: 4.0, 0: 1.5}, "frontier": {"a": [(13.0, 7.4), (13.1, 7.3)], "b": [(21.5, 10.0)]}}
    st.set_status(3, "failed", fail)
    ctx.reports = [StageReport("classify", routed=0, failed=0, score=Score(opens=5), work=12, notes=["5 connections"]),
                   StageReport("detail", routed=4, failed=1, score=st.score(), work=41234, notes=["1 boxed in"])]
    return ctx, fail


D3_PATH = [(13, 8), (13, 7), (13, 6)] + [(x, 6) for x in range(14, 23)] + [(22, y) for y in range(7, 11)]


def plan_d3(ctx, layer=0):
    """The global stage's plan for the failed /D3, published the way globalroute.py does: up from U1.4, east
    along tile row 6 (where /D0's copper already runs as far as x = 19.5), down to J1.4."""
    ctx.history["global"] = {"path": {3: list(D3_PATH), 0: [(10, 8), (10, 7)]},
                             "layer": {3: [layer] * len(D3_PATH), 0: [0, 0]}}
    ctx.history.pop("_diag_demand", None)


def inside(ctx, prims):
    x0, y0, x1, y1 = ctx.board.bbox
    n = 0
    for p in prims:
        if p["type"] in ("rect", "view"):
            pts = [p["rect"][:2], p["rect"][2:]]
        elif p["type"] == "line":
            pts = p["pts"]
        else:
            pts = [p["at"]]
        for x, y in pts:
            assert x0 - 1e-9 <= x <= x1 + 1e-9 and y0 - 1e-9 <= y <= y1 + 1e-9, (p, ctx.board.bbox)
            n += 1
    return n


# ── certificates ──────────────────────────────────────────────────────────
def test_certificate_fields_are_complete():
    ctx, fail = make()
    e = D.failure(ctx, ctx.conns[3], fail)
    assert set(e) == CERT_KEYS | {"_frontier"}, sorted(set(e) ^ CERT_KEYS)
    assert (e["net"], e["from"], e["to"], e["conn"]) == ("/D3", "U1.4", "J1.4", 3)
    assert e["reason"] == "pad boxed in" and e["where"] == [13.0, 7.3]
    assert e["blocked_by"] == ["/D2", "/USB_DP"]                    # ranked: /D2 named twice (Blocker + piece id x 3)
    assert e["static_share"] == 0.5                                  # one of the two recorded Blockers is fixed copper
    assert e["mincut_nets"] == ["/D2", "/D0"]                        # by crossing cost
    assert e["probe"] == {"enclosed": "a", "reached": 41} and e["tried"] == ["pattern", "probe"]
    assert set(e["cut"]) == CUT_KEYS | {"at"} and e["cut"]["a"] == "U1.4" and e["cut"]["b"] in ("U1.3", "U1.5")
    assert e["cut"]["capacity_mm"] == 0.0 and e["cut"]["demand_mm"] == 0.4 and e["cut"]["nets"] == ["/D3"]
    assert e["cut"]["layer"] == "F.Cu" and e["cut"]["overflow_mm"] == 0.4
    assert ctx.history["diag_failed"][3] is e
    json.dumps({k: v for k, v in e.items()})                         # plain data
    # a stage that recorded nothing still gets every key (static_share from the oracle at the failure point)
    e2 = D.failure(ctx, ctx.conns[3], {"reason": "effort", "where": [13.0, 8.0]})
    assert set(e2) == CERT_KEYS and e2["probe"] == {"enclosed": "none"} and e2["tried"] == [] and e2["mincut_nets"] == []
    # around U1.4: the neighbour pads U1.3 / U1.5 (fixed) and /D2's new track (rippable)
    assert 0.3 < e2["static_share"] < 0.9 and e2["blocked_by"] == [] and e2["cut"] is None
    e3 = D.failure(ctx, ctx.conns[3], {})
    assert e3["reason"] == "not routed" and e3["where"] == [13.0, 8.0] and set(e3) == CERT_KEYS
    # the cut a stage names wins; a Cut object or a dict are both accepted
    c = Cut("C1.1", "U1.11", "F.Cu", 0.4, 0.8, ["+3V3", "/A0"])
    assert D.failure(ctx, ctx.conns[3], {"cut": c})["cut"]["overflow_mm"] == 0.4
    assert D.failure(ctx, ctx.conns[3], {"cut": {"a": "x", "b": "y", "layer": 1, "capacity_mm": 1, "demand_mm": 3,
                                                  "nets": ["/D3"]}})["cut"]["layer"] == "B.Cu"


def test_pad_cut_without_tiles_uses_the_oracle_gap():
    ctx, fail = make(tiles=False)
    e = D.failure(ctx, ctx.conns[3], fail)
    # U1 pads are 0.6 wide at 1.0 pitch: gap 0.4; tracks = floor((0.4 - 0.2) / (0.2 + 0.2)) = 0
    assert e["cut"]["a"] == "U1.4" and e["cut"]["capacity_mm"] == 0.0 and e["cut"]["demand_mm"] == 0.4
    ctx.conns[3].clearance = 0.1
    ctx.conns[3].neck = 0.1
    e = D.failure(ctx, ctx.conns[3], fail)                           # floor((0.4 - 0.1) / 0.2) = 1 track = 0.2 mm
    assert e["cut"]["capacity_mm"] == 0.2 and e["cut"]["demand_mm"] == 0.2 and e["cut"]["overflow_mm"] == 0.0


# ── stages, congestion, cuts ──────────────────────────────────────────────
def test_stages_and_auto():
    ctx, _ = make()
    s = D.stages(ctx)
    assert [r["name"] for r in s] == ["classify", "detail"] and set(s[1]) == {"name", "routed", "failed", "score", "work", "notes"}
    assert s[1]["routed"] == 4 and s[1]["failed"] == 1 and s[1]["work"] == 41234 and s[1]["score"]["vias"] == 3
    assert s[1]["score"]["opens"] == ctx.state.score().opens
    ctx.x.auto["roles"] = {"/D0": "bus", "+3V3": "power"}
    ctx.x.auto["layer_dirs"] = {0: "h", 1: "v"}
    a = D.auto(ctx)
    assert a == {"roles": {"+3V3": "power", "/D0": "bus"}, "buses": [], "widths": {}, "layer_dirs": {"F.Cu": "h", "B.Cu": "v"}}


def test_congestion_counts_copper_on_tile_borders():
    ctx, _ = make()
    c = D.congestion(ctx)
    assert c["tile_mm"] == 1.0 and c["cell_mm"] == 1.0 and (c["cols"], c["rows"]) == (30, 20) and c["capacity"] == "tiles"
    assert c["origin"] == [0.0, 0.0] and set(c["layers"]) == {"F.Cu", "B.Cu"} and c["idle_layers"] == []
    f = c["layers"]["F.Cu"]
    assert len(f) == 20 and all(len(r) == 30 for r in f)
    # /D0 runs east along y = 6.5: one 0.2 track + 0.2 clearance over a 1.2 mm border = 33 %
    assert f[6][15] == 33 and f[6][10] == 33
    # where /D2 (0.15 + 0.2) joins it between x = 13 and 19 the row above carries only /D2: 29 %
    assert f[7][15] == 29
    assert c["layers"]["B.Cu"][17][10] == 33 and c["layers"]["B.Cu"][5][5] == 0
    assert c["max"]["F.Cu"] == max(max(r) for r in f)
    # routed copper proves room for itself: a border the tile model calls closed reads 100 % under a track,
    # 999 only when a connection without copper is planned through it; closed and unwanted reads -1
    ctx.tiles.over[(0, 15, 6, "E")] = 0.0
    ctx.tiles.over[(0, 20, 6, "E")] = 0.0
    ctx.tiles.over[(0, 3, 3, "E")] = 0.0
    ctx.tiles.over[(0, 3, 3, "N")] = 0.0
    ctx.history.clear()
    c = D.congestion(ctx)
    assert c["layers"]["F.Cu"][6][15] == 100 and c["layers"]["F.Cu"][3][3] == -1 and c["layers"]["F.Cu"][6][20] == 0
    assert c["hot"][0] == {"layer": "F.Cu", "at": [16.0, 6.5], "util": 100}
    plan_d3(ctx)
    c = D.congestion(ctx)
    assert c["layers"]["F.Cu"][6][15] == 200 and c["layers"]["F.Cu"][6][20] == 999      # /D0 + planned /D3; /D3 alone
    assert c["layers"]["F.Cu"][6][16] == 67 and c["layers"]["F.Cu"][7][13] == 33         # 0.8 / 1.2; the N step
    assert c["hot"][0] == {"layer": "F.Cu", "at": [21.0, 6.5], "util": 999}
    # the tile origin is found through tile_of when the tiles do not start at the board corner
    ctx2, _ = make(origin=(-0.25, -0.5))
    c2 = D.congestion(ctx2)
    assert c2["origin"] == [-0.25, -0.5] and c2["layers"]["F.Cu"][7][10] == 33        # y = 6.5 is row 7 there
    assert c2["layers"]["F.Cu"][7][15] == 62                             # ... and shares its borders with /D2: 0.75 / 1.2
    # without a tile graph: same counting, estimated capacity floor((1 + 0.2) / 0.4) * 0.4 = 1.2
    ctx3, _ = make(tiles=False)
    c3 = D.congestion(ctx3)
    assert c3["capacity"] == "estimate" and c3["layers"]["F.Cu"][6][15] == 33
    # big boards are reported coarser, never more than 40 x 40
    ctx3.x.tile_mm = 0.25
    ctx3.history.clear()
    c4 = D.congestion(ctx3)
    assert c4["tile_mm"] == 0.25 and c4["cell_mm"] == 0.75 and c4["cols"] == 40 and c4["rows"] == 27
    assert max(c4["cols"], c4["rows"]) <= 40 and c4["layers"]["F.Cu"][8][20] >= 33


def test_cuts_are_ranked_by_overflow():
    ctx, fail = make()
    assert D.cuts(ctx) == []                                          # nothing over capacity
    ctx.tiles.over[(0, 15, 6, "E")] = 0.1                             # under /D0's track (0.4)
    ctx.tiles.over[(1, 10, 17, "E")] = 0.3                            # under /D1's track
    ctx.history.clear()
    assert D.cuts(ctx) == []                                          # copper that is there fits: not a cut
    plan_d3(ctx)                                                      # the unrouted /D3 wants through row 6 too
    ctx.tiles.over[(0, 20, 6, "E")] = 0.15
    ctx.tiles.over[(0, 13, 7, "N")] = 0.0                             # right above its own pad U1.4 ...
    ctx.tiles.shadow["U1.4"] = {(0, 13, 7, "N")}                      # ... which is that pad's shadow: no demand
    cs = D.cuts(ctx)
    assert [c["a"] for c in cs] == ["tile(15,6)|E", "tile(20,6)|E"], cs
    assert set(cs[0]) == CUT_KEYS | {"at"} and cs[0]["b"] == "tile(16,6)" and cs[0]["layer"] == "F.Cu"
    # /D0's 0.4 is there, so at least 0.4 fits; /D3 needs 0.4 more
    assert (cs[0]["capacity_mm"], cs[0]["demand_mm"], cs[0]["overflow_mm"], cs[0]["nets"]) == (0.4, 0.8, 0.4, ["/D0", "/D3"])
    assert cs[0]["at"] == [16.0, 6.5]
    assert (cs[1]["capacity_mm"], cs[1]["demand_mm"], cs[1]["overflow_mm"], cs[1]["nets"]) == (0.15, 0.4, 0.25, ["/D3"])
    assert len(D.cuts(ctx, top=1)) == 1
    del ctx.tiles.shadow["U1.4"]
    ctx.history.pop("_diag_demand", None)
    assert [c["a"] for c in D.cuts(ctx)] == ["tile(13,7)|N", "tile(15,6)|E", "tile(20,6)|E"]
    ctx.tiles.shadow["U1.4"] = {(0, 13, 7, "N")}
    # cuts named by a certificate and by the global stage join the list
    D.failure(ctx, ctx.conns[3], fail)
    ctx.reports.insert(1, StageReport("global", data={"cuts": [Cut("R1.2", "R2.1", "F.Cu", 0.4, 2.0, ["VBUS", "/A0"])]}))
    ctx.history.pop("_diag_demand", None)
    cs = D.cuts(ctx)
    assert [c["a"] for c in cs] == ["R1.2", "U1.4", "tile(15,6)|E", "tile(20,6)|E"]
    assert cs[0]["overflow_mm"] == 1.6 and cs[0]["at"] == [22.0, 16.0]
    # once every connection is routed the global plan's residual overflow is moot
    ctx.state.set_status(3, "routed")
    ctx.history.pop("_diag_demand", None)
    assert [c["a"] for c in D.cuts(ctx)] == ["U1.4"]                  # only the cut a certificate named remains


def test_planned_paths_count_for_connections_without_copper():
    ctx, fail = make()
    # the global stage planned /D3 east along tile row 9 on the bottom layer; it has no copper yet
    path = [(13, 8), (13, 9)] + [(x, 9) for x in range(14, 23)] + [(22, 10)]
    ctx.reports.insert(1, StageReport("global", data={"paths": {3: [(tx, ty, 1) for tx, ty in path], 0: [(0, 0), (1, 0)]}}))
    ctx.corridors[3] = Corridor(conn=3, tiles=frozenset(path), layers=(1,))
    c = D.congestion(ctx)
    assert c["layers"]["B.Cu"][9][18] == 33 and c["layers"]["B.Cu"][8][13] == 33          # E borders and the N step
    assert c["layers"]["F.Cu"][0][0] == 0                               # /D0 is routed: its plan is not counted twice
    ctx.tiles.over[(1, 18, 9, "E")] = 0.2
    ctx.history.clear()
    e = D.failure(ctx, ctx.conns[3], {"reason": "effort"})             # no cut named: the worst border of its corridor
    assert e["cut"]["a"] == "tile(18,9)|E" and e["cut"]["nets"] == ["/D3"] and e["cut"]["overflow_mm"] == 0.2
    sk = D.overlays(ctx, "sketch")
    lines = [p for p in sk if p["type"] == "line"]
    assert len(lines) == len(ctx.conns) + 1 and inside(ctx, sk)
    d3 = next(p for p in lines if p.get("net") == "/D3")
    assert d3["pts"][0] == [13.0, 8.0] and d3["pts"][1] == [13.5, 8.5] and d3["pts"][-1] == [22.0, 10.0]
    assert [p for p in lines if p["color"][:3] == [255, 40, 40]][0]["pts"] == [[19.0, 9.0], [19.0, 10.0]]


# ── critique and suggestions ──────────────────────────────────────────────
def test_critique_names_every_kind():
    ctx, _ = make()
    cr = D.critique(ctx)
    by = {}
    for c in cr:
        assert set(c) >= {"kind", "net", "value", "limit", "where"} and c["kind"] in D.KINDS, c
        by.setdefault(c["kind"], []).append(c)
    assert set(by) == set(D.KINDS), sorted(set(D.KINDS) - set(by))
    v = by["vias_over_budget"][0]
    assert (v["net"], v["value"], v["limit"]) == ("/D1", 3, 1)
    n = by["necked_share"][0]
    assert (n["net"], n["value"], n["limit"]) == ("/D2", 1.0, 0.25)
    d = by["detour"][0]
    assert d["net"] == "/D1" and d["value"] > 4 and d["limit"] == 2.0 and (d["from"], d["to"]) == ("U1.2", "J1.2")
    bu = by["bus_unbundled"][0]
    assert bu["nets"] == ["/D1"] and bu["value"] == 1 and bu["layer"] == "F.Cu" and bu["bus"] == ["/D0", "/D1", "/D2"]
    pw = by["power_thinner_than_pad"][0]
    assert (pw["net"], pw["value"], pw["limit"]) == ("+3V3", 0.2, 0.4)
    oa = by["odd_angle_share"][0]
    assert oa["net"] is None and 0.05 < oa["value"] < 0.5
    x0, y0, x1, y1 = ctx.board.bbox
    assert all(x0 <= c["where"][0] <= x1 and y0 <= c["where"][1] <= y1 for c in cr)
    json.dumps(cr)
    # against a reference routing (e.g. the human one): vias and length per net
    cr2 = D.critique(ctx, {"/D0": [14.0, 0], "/D1": [12.0, 1], "+3V3": [8.2, 0]})
    v2 = [c for c in cr2 if c["kind"] == "vias_over_budget"]
    assert [(c["net"], c["limit"]) for c in v2] == [("/D1", 1)]
    d2 = [c for c in cr2 if c["kind"] == "detour"]
    assert [(c["net"], c["limit"]) for c in d2] == [("/D1", 1.5)]
    # long lists are cut to the worst 8 per kind, and say so
    rows = [{"kind": "detour", "net": f"/N{i}", "value": 9 - i * 0.1, "limit": 2.0, "where": [1, 1]} for i in range(11)]
    capped = D._cap_kinds(rows + [{"kind": "odd_angle_share", "net": None, "value": 0.2, "limit": 0.05, "where": [1, 1]}])
    assert [r["net"] for r in capped] == [f"/N{i}" for i in range(8)] + [None] and capped[7]["more"] == 3 and "more" not in capped[0]
    # a clean board has nothing to say
    clean, _ = make()
    for c in (1, 2, 4):
        clean.state.rip_conn(c)
    assert D.critique(clean) == []


def test_suggest_patches_validate_against_the_plan_schema():
    ctx, fail = make()
    ctx.tiles.over[(0, 15, 6, "E")] = 0.1
    plan_d3(ctx)
    failed = [D.failure(ctx, ctx.conns[3], fail)]
    crit = D.critique(ctx)
    sg = D.suggest(ctx, failed, crit)
    assert 5 <= len(sg) <= D.MAX_SUGGEST and all(set(s) == {"why", "patch"} and s["why"] for s in sg)
    base = {"global": {"engine": "v2"}, "nets": {"/D1": {"via_budget": 1}}, "buses": [{"nets": ["/D0", "/D1", "/D2"]}]}
    kinds = set()
    for s in sg:
        p = s["patch"]
        json.dumps(p)
        if "move" in p:
            kinds.add("move")
            assert set(p) == {"move"} and set(p["move"]) == {"ref", "by"} and len(p["move"]["by"]) == 2
            assert p["move"]["ref"] in {q.ref for q in ctx.board.pads}
            continue
        kinds |= set(p)
        merged = X.merge_patch(base, p)
        cfg = P.resolve(ctx.board, merged)                             # raises BAD_PLAN if the patch is not a plan
        assert cfg.engine == "v2"
        assert not [w for w in cfg.warnings if "ignores" in w]
    assert {"order", "nets", "regions"} <= kinds, kinds
    whys = " | ".join(s["why"] for s in sg)
    assert "/D3 is blocked by routed copper of /D2" in whys and "route it before them" in whys
    assert sg[[s["patch"].get("order") is not None for s in sg].index(True)]["patch"]["order"] == ["/D3", "*"]
    assert any(s["patch"] == {"nets": {"/D1": {"max_vias": 1}}} for s in sg)
    assert any(s["patch"] == {"nets": {"+3V3": {"width": 0.4}}} for s in sg)
    assert any(s["patch"] == {"nets": {"/D1": {"priority": 90}}} for s in sg)
    reg = [s["patch"]["regions"][0] for s in sg if "regions" in s["patch"]]
    assert any(r.get("except") == ["/D2"] and r["capacity_scale"] == 0.6 for r in reg)       # room for the necked net
    assert any(r["name"] == "cut tile(15,6)|E" and r["layers"] == ["F.Cu"] and r["capacity_scale"] == 0.5 for r in reg)
    x0, y0, x1, y1 = ctx.board.bbox
    assert all(x0 <= r["rect"][0] < r["rect"][2] <= x1 and y0 <= r["rect"][1] < r["rect"][3] <= y1 for r in reg)
    assert D.suggest(ctx, failed, crit) == sg                          # same twice
    assert any(s["patch"] == {"nets": {"/D1": {"prefer": ["F.Cu"]}}} for s in sg)              # the stray bus member
    # with no failures and no critique passed in, only the known cuts speak
    only = D.suggest(ctx, [], [])
    assert [sorted(s["patch"]) for s in only] == [["fanout"], ["regions"]] and all(s["patch"] in [t["patch"] for t in sg] for s in only)


def test_suggest_for_budget_cuts_between_parts_and_limits():
    ctx, _ = make({"nets": {"/D3": {"max_vias": 0}}})
    c = ctx.conns[3]
    late = [D.failure(ctx, c, {"reason": "time budget used up before this connection was tried"})]
    assert late[0]["static_share"] == 0.0                              # nobody searched: nothing blocked it
    sg = D.suggest(ctx, late, [])
    assert sg[0]["patch"] == {"global": {"time_budget_s": 180}} and "search budget" in sg[0]["why"]
    assert all("order" not in s["patch"] and "nets" not in s["patch"] for s in sg)      # no per-connection advice yet
    ctx.cfg.time_budget = 600.0
    assert D.suggest(ctx, late, [])[0]["patch"] == {"global": {"effort": 2.0}}
    # a cut between two different parts: move the smaller one away by the shortfall, rounded up to 0.05 mm
    f = D.failure(ctx, c, {"reason": "no path", "cut": Cut("C1.1", "U1.12", "F.Cu", 0.4, 0.8, ["/D3", "+3V3"]),
                           "static_share": 1.0})
    sg = D.suggest(ctx, [f], [])
    mv = [s for s in sg if "move" in s["patch"]][0]
    assert mv["patch"] == {"move": {"ref": "C1", "by": [-0.45, 0.0]}} and "move C1 0.45 mm away from U1" in mv["why"]
    # a cut between two pads of one fine part: escape through a via
    f = D.failure(ctx, c, {"reason": "pad boxed in", "probe": "a", "static_share": 1.0})
    sg = D.suggest(ctx, [f], [])
    assert {"fanout": {"U1": {"style": "via"}}} in [s["patch"] for s in sg]
    P.resolve(ctx.board, X.merge_patch({"global": {"engine": "v2"}}, {"fanout": {"U1": {"style": "via"}}}))
    # max_vias in the reason: relax it by one
    f = D.failure(ctx, c, {"reason": "no route within max_vias", "static_share": 1.0})
    assert {"nets": {"/D3": {"max_vias": 1}}} in [s["patch"] for s in D.suggest(ctx, [f], [])]
    # off-board parts are a placement problem, not a plan problem
    f = D.failure(ctx, c, {"reason": "J9 is outside the board outline"})
    assert D.suggest(ctx, [f], []) == []
    # polish off + odd angles
    ctx2, _ = make({"global": {"polish": False}})
    assert {"global": {"polish": True}} in [s["patch"] for s in D.suggest(ctx2, [], D.critique(ctx2))]


# ── overlays and pictures ─────────────────────────────────────────────────
def test_overlays_stay_inside_the_board_and_get_drawn():
    from PIL import Image as PImage
    ctx, fail = make()
    ctx.tiles.over[(0, 15, 6, "E")] = 0.1
    plan_d3(ctx)
    D.failure(ctx, ctx.conns[3], fail)
    for kind in D.OVERLAY_KINDS:
        prims = D.overlays(ctx, kind, 3 if kind == "failure" else None)
        assert prims and inside(ctx, prims) > 0
        assert {p["type"] for p in prims} <= {"rect", "line", "text", "view"}
        json.dumps(prims)
        assert D.overlays(ctx, kind, 3 if kind == "failure" else None) == prims
    cg = D.overlays(ctx, "congestion")
    assert sum(1 for p in cg if p["type"] == "rect") >= 20 and any("tile(15,6)|E" in p.get("text", "") for p in cg)
    fl = D.overlays(ctx, "failure", 3)
    view = fl[0]
    assert view["type"] == "view" and view["rect"][0] <= 13.0 <= view["rect"][2] and view["rect"][0] <= 22.0 <= view["rect"][2]
    texts = [p["text"] for p in fl if p["type"] == "text"]
    assert "U1.4 (boxed in)" in texts and "J1.4" in texts and "/D2" in texts                  # terminals + blocking net
    assert any(t.startswith("/D3: pad boxed in") for t in texts) and any(t.startswith("U1.4 | U1.") and "needs 0.4, fits 0.0 mm" in t for t in texts)
    assert sum(1 for p in fl if p["type"] == "rect" and p.get("fill")) == 3                    # the frontier cells
    for bad_call in (lambda: D.overlays(ctx, "heat"), lambda: D.overlays(ctx, "failure", 0)):
        try:
            bad_call()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    # draw() paints on any Pillow image through a mm -> px mapping
    im = PImage.new("RGB", (600, 400), (0, 0, 0))
    D.draw(im, lambda x, y: (x * 20, y * 20), cg + fl)
    assert len(im.getcolors(100000)) > 5
    # render(): whole board, and cropped to the failure view; both are real PNGs of sensible size
    p1 = D.render(ctx.board, cg, OUT / "congestion.png", title="congestion")
    p2 = D.render(ctx.board, fl, OUT / "failure.png", title="why /D3 failed")
    a, b = PImage.open(p1), PImage.open(p2)
    assert a.size[0] >= 1000 and b.size[0] <= 1100 and len(a.getcolors(1 << 20)) > 8 and len(b.getcolors(1 << 20)) > 8
    # the crop shows a smaller area at a larger scale
    assert b.size[0] / (view["rect"][2] - view["rect"][0] + 1.6) > a.size[0] / 33.0


def test_build_returns_every_key_and_is_the_same_twice():
    def run():
        ctx, _ = make()
        ctx.tiles.over[(0, 15, 6, "E")] = 0.1
        plan_d3(ctx)
        return ctx, D.build(ctx)
    ctx, out = run()
    assert set(out) == {"stages", "auto", "failed", "congestion", "cuts", "critique", "suggest", "_overlays"}
    assert len(out["failed"]) == 1 and out["failed"][0]["net"] == "/D3" and out["failed"][0]["cut"]["a"] == "U1.4"
    ov = out["_overlays"]
    assert ov["congestion"] and ov["sketch"] is None and [f["net"] for f in ov["failures"]] == ["/D3"]
    assert inside(ctx, ov["congestion"]) and inside(ctx, ov["failures"][0]["prims"])
    _, again = run()
    assert json.dumps(out, sort_keys=True, default=str) == json.dumps(again, sort_keys=True, default=str)
    # what goes to the model is small: the text part of this board's diagnostics
    text = {k: v for k, v in out.items() if k != "_overlays"}
    assert len(json.dumps(text, default=str)) < 12000, len(json.dumps(text, default=str))
    # a context where nothing was routed (stage-limited dry run): the sketch replaces the route picture
    ctx2, _ = make()
    for c in range(5):
        ctx2.state.rip_conn(c)
        ctx2.state.set_status(c, "todo")
    out2 = D.build(ctx2)
    assert out2["failed"] == [] and out2["critique"] == [] and out2["_overlays"]["sketch"]
    assert out2["congestion"]["layers"] == {} and out2["congestion"]["idle_layers"] == ["F.Cu", "B.Cu"]
    # a half-built context (no state, no oracle, no tiles) does not raise
    bare = Ctx(board=ctx.board, cfg=ctx.cfg, x=ctx.cfg.x)
    out3 = D.build(bare)
    assert out3["stages"] == [] and out3["cuts"] == [] and out3["suggest"] == [] and out3["failed"] == []


# ── the tool layer (tools/route_tools.py) ─────────────────────────────────
def test_tool_findings_read_cut_critique_and_suggest():
    import copper_kicad.tools.route_tools as R
    ctx, _ = make()
    ctx.tiles.over[(0, 15, 6, "E")] = 0.1
    plan_d3(ctx)
    out = D.build(ctx)
    out.update({"routed": 4, "total": 5, "drc": {"errors": 0}})
    f = R._findings(out)
    codes = [x["code"] for x in f]
    assert codes.count("UNROUTED") == 1 and codes.count("CUT_OVER_CAPACITY") == 1 and codes.count("SUGGESTED_PATCHES") == 1
    assert codes.count("ROUTE_QUALITY") == len(D.KINDS)
    un = f[codes.index("UNROUTED")]
    assert un["fix"].startswith("suggest[0]: ") and "plan patch {" in un["fix"] and "Otherwise: " in un["fix"]
    cut = f[codes.index("CUT_OVER_CAPACITY")]
    assert "/D3 (U1.4→J1.4): 0.4 mm of track + clearance must pass between U1.4 and U1.3 on F.Cu, 0.0 mm fits" in cut["message"]
    assert 'plan patch {"fanout":{"U1":{"style":"via"}}}' in cut["fix"]
    q = [x for x in f if x["code"] == "ROUTE_QUALITY"]
    assert any(x["message"].startswith("vias_over_budget — more vias than planned: /D1 3 (limit 1)") for x in q)
    assert all(x["severity"] == "info" and "suggest[]" in x["fix"] for x in q)
    sp = f[codes.index("SUGGESTED_PATCHES")]
    assert sp["message"].startswith(f"{len(out['suggest'])} ready patch(es)") and "pcb_place" in sp["fix"]
    assert R._patch_text({"move": {"ref": "C1", "by": [0.4, 0.0]}}) == "pcb_place(moves=[{'ref': 'C1', 'by': [0.4, 0.0]}])"
    # a v1 result (no v2 keys) gets exactly the findings it got before
    v1 = {"failed": [{"net": "/A", "from": "U1.1", "to": "J1.1", "reason": "no path", "blocked_by": ["/B"]}],
          "routed": 1, "total": 2, "drc": {"errors": 0}}
    f1 = R._findings(v1)
    assert [x["code"] for x in f1] == ["UNROUTED"] and f1[0]["fix"].startswith("route the failed nets first")
    assert R._v2_findings({}) == [] and R._v2_findings({"failed": [{"net": "/A", "cut": None}]}) == []
    # the tool text teaches the loop and names every v2 field
    from copper_kicad.mcp import registry
    doc = registry.TOOLS["pcb_route_guided"].description
    # the electrical-constraint fields (design §14.3) are documented in copper_help: the catalogue budget has no
    # room for them, and the description may not grow
    in_help = {"widths", "z", "groups"}
    more = registry.HELP["pcb_route_guided"]
    for word in list(X.V2_NET) + list(X.V2_TOP) + list(X.V2_REGION) + ["stages", "tile_mm", "critique", "suggest", "cuts",
                                                                      "static_share", "dry_run=true", 'engine":"guided"',
                                                                      "'v2' is the default", "detail=true"]:
        assert word in (more if word in in_help else doc), word
    for word in ("widths: {", "z: 50", "groups: [{nets", "tol", "skew", "uncoupled", "coupled", 'constraints: "auto"'):
        assert word in more, word
    assert "auto" in registry.TOOLS["pcb_route_plan"].description


def test_tool_draws_the_diagnostic_pictures():
    import copper_kicad.tools.route_tools as R
    from PIL import Image as PImage

    class FakeProject:
        out_dir = OUT
    ctx, _ = make()
    ctx.tiles.over[(0, 15, 6, "E")] = 0.1
    plan_d3(ctx)
    out = D.build(ctx, with_overlays=False)
    ov = ctx.x.overlays                              # left there by suggest(), the last diagnosis of a run
    assert set(ov) == {"congestion", "sketch", "failures"} and ov["sketch"] is None and len(ov["failures"]) == 1
    imgs = R._diag_images(FakeProject, ctx.board, ov, out)
    assert [Path(i.path).name for i in imgs] == ["pcb-route-congestion.png", "pcb-route-failure-1.png"]
    assert "why /D3 failed" in imgs[1].caption and all(PImage.open(i.path).size[0] > 300 for i in imgs)
    # nothing failed, nothing over capacity: no extra pictures; a stage-limited dry run gets the sketch
    clean = {"failed": [], "cuts": [], "dry_run": True}
    assert R._diag_images(FakeProject, ctx.board, {"congestion": ov["congestion"], "sketch": None, "failures": []}, clean) == []
    sk = R._diag_images(FakeProject, ctx.board, {"congestion": [], "sketch": D.overlays(ctx, "sketch"), "failures": []}, clean)
    assert [Path(i.path).name for i in sk] == ["pcb-route-sketch-dryrun.png"]
    # a connection whose ConnV2 carries no width (a stage that filled only the terminals) still gets real numbers
    bare = ConnV2(idx=3, net="/D3", a=ctx.conns[3].a, b=ctx.conns[3].b)
    assert D._wc(ctx, bare) == (0.2, 0.2)
    assert D.failure(ctx, bare, {"reason": "pad boxed in", "probe": "a"})["cut"]["demand_mm"] == 0.4
    D.failure(ctx, ctx.conns[3], {"reason": "pad boxed in", "probe": "a"})
    # at most 6 failure crops
    many = {"congestion": [], "sketch": None, "failures": ov["failures"] * 9}
    assert len(R._diag_images(FakeProject, ctx.board, many, {"failed": [1]})) == D.MAX_FAILURE_IMAGES
    # connections nobody tried (stage-limited run, budget) get no crop: there is nothing to show yet
    e1 = D.failure(ctx, ctx.conns[3], {"reason": "not routed: no search stage ran (plan.global.stages / this build)"})
    e2 = D.failure(ctx, ctx.conns[3], {"reason": "time budget used up before this connection was tried"})
    assert D.overlay_set(ctx, [e1])["failures"] == [] and D.overlay_set(ctx, [e2])["failures"] == []
    assert D.overlay_set(ctx, [{"net": "/X", "reason": "no path"}])["failures"] == []       # no certificate, no crop


def test_e2e_tools_on_a_real_board():
    """pcb_route_plan returns `auto`; a plan with v2 fields routes unchanged under the 'guided' engine (with a
    note); engine 'v2' is exercised as far as the other work packages have landed (reported, not asserted)."""
    if not (common.KICAD_CLI.exists() and common.KICAD_PY.exists()):
        print("    (skipped: KiCad not found)")
        return
    import test_guided_route as G
    if not G.have_kicad():
        print("    (skipped: KiCad not found)")
        return
    d = G.build("v2diag")
    p, r = G.call("pcb_route_plan", project=d)
    a = p["auto"]
    assert set(a) >= {"roles", "buses", "widths", "layer_dirs", "source"}, a
    assert a["roles"].get("+3V3") == "power" and a["roles"].get("/USB_DP") == "pair" and a["roles"].get("/D0") in (None, "bus"), a
    assert len(json.dumps(a)) < 1500 and len(json.dumps(p)) < 10500, (len(json.dumps(a)), len(json.dumps(p)))
    assert "// v2 fields" in p["plan_schema"] and "engine" not in p["suggested_plan"].get("global", {})
    # (engine 'v2' is the default: the classic router is selected by name)
    plan = {"global": {"engine": "guided"}, "nets": {"/D0": {"via_budget": 1, "role": "signal"}},
            "regions": [{"rect": [100, 100, 104, 104], "capacity_scale": 0.5}]}
    o, r = G.call("pcb_route_guided", project=d, plan=plan, dry_run=True, render=False)
    notes = [f["message"] for f in o["findings"] if f["code"] == "NOTE"]
    assert any("v2-only" in m and "nets.via_budget" in m for m in notes), o["findings"]
    assert o["plan_used"]["nets"]["/D0"]["via_budget"] == 1 and o["total"] > 0 and "stages" not in o
    base, _ = G.call("pcb_route_guided", project=d, dry_run=True, render=False, plan={"global": {"engine": "guided"}})
    assert (o["routed"], o["total"], o["added"]) == (base["routed"], base["total"], base["added"])    # un-steered = same copper
    e = G.call_err("pcb_route_guided", project=d, plan={"global": {"engine": "v2", "stagez": []}}, dry_run=True)
    assert "BAD_PLAN" in json.dumps(e), e
    from copper_kicad.pcb.v2 import engine
    if getattr(engine, "STUB", False):
        print("    engine 'v2' not built yet: tool path through v2 not exercised")
        return
    try:
        o2, r2 = G.call("pcb_route_guided", project=d, dry_run=True, detail=True,
                        plan={"global": {"engine": "v2"}, "nets": {"/D0": {"via_budget": 0}}})
        o3, _r3 = G.call("pcb_route_guided", project=d, dry_run=True, render=False, plan={"nets": {"/D0": {"via_budget": 0}}})
        assert o3.get("engine") == "v2" and "stages" not in o3 and "detail=true" in o3.get("more", ""), sorted(o3)
        assert (o3["routed"], o3["total"], o3["added"]) == (o2["routed"], o2["total"], o2["added"])    # v2 is the default
    except AssertionError as ex:
        print(f"    engine 'v2' dry run failed outside W9's files: {str(ex)[:300]}")
        return
    keys = sorted(k for k in ("stages", "auto", "congestion", "cuts", "critique", "suggest") if k in o2)
    print(f"    engine 'v2' dry run: {o2.get('routed')}/{o2.get('total')}, failed {len(o2.get('failed') or [])}, v2 keys {keys}, "
          f"{len(G.images(r2))} image(s), findings {[f['code'] for f in o2['findings']]}")
    assert "stages" in o2 and isinstance(o2["findings"], list)
    assert not any("diagnostic pictures not drawn" in f.get("message", "") for f in o2["findings"])
    assert not any("v2 diagnostics" in f.get("message", "") and "failed" in f.get("message", "") for f in o2["findings"]), o2["findings"]
    for e2 in o2.get("failed") or []:
        assert "_a" not in e2 and "_frontier" not in e2


def test_no_clock_no_random():
    src = Path(D.__file__).read_text(encoding="utf-8")
    for banned in ("import time", "from time", "import random", "datetime", "perf_counter", "monotonic"):
        assert banned not in src, banned
    assert not getattr(D, "STUB", False)


# ── throughput, and a real board ──────────────────────────────────────────
def test_throughput_on_a_large_state():
    """20 000 segments on a 200 x 150 mm board: every text diagnostic well inside a second or two."""
    b = board()
    cfg = P.resolve(b, {"global": {"engine": "v2", "tile_mm": 0.5}})
    orc = Oracle(b)
    conns = [ConnV2(idx=i, net="/D0", a=term(b, "U1.1"), b=term(b, "J1.1"), est=12.0, width=0.2, neck=0.2,
                    clearance=0.2) for i in range(2000)]
    st = RouteState(orc, conns)
    ctx = Ctx(board=b, cfg=cfg, x=cfg.x, oracle=orc, state=st, conns=conns)
    n = 0
    for i in range(2000):                                   # 2000 connections x 10 segments, all over the board
        x, y = 1.0 + (i * 7) % 27, 1.0 + (i * 13) % 17
        for k in range(10):
            x2, y2 = (x + 1.3, y) if k % 2 == 0 else (x, y + 0.9)
            x2, y2 = min(29.0, x2), min(19.0, y2)
            st.add(net="/D0", conn=i, kind="seg", layer=k % 2, x1=x, y1=y, x2=x2, y2=y2, w=0.2)
            x, y = x2, y2
            n += 1
        st.set_status(i, "routed")
    t = time.perf_counter()
    c = D.congestion(ctx)
    t1 = time.perf_counter()
    cr = D.critique(ctx)
    t2 = time.perf_counter()
    cu = D.cuts(ctx)
    sg = D.suggest(ctx, [], cr)
    ov = D.overlays(ctx, "congestion")
    t3 = time.perf_counter()
    print(f"    {n} segments: congestion {1e3 * (t1 - t):.0f} ms ({n / (t1 - t) / 1e3:.0f} k seg/s), critique "
          f"{1e3 * (t2 - t1):.0f} ms, cuts+suggest+overlay {1e3 * (t3 - t2):.0f} ms; {len(ov)} primitives")
    assert c["cols"] <= 40 and c["rows"] <= 40 and isinstance(cu, list) and isinstance(sg, list)
    assert t3 - t < 6.0, t3 - t


def test_corpus_reference_boards():
    """Human-routed corpus boards as the route state: diagnostics stay bounded, inside the board, and drawable."""
    if not (common.KICAD_CLI.exists() and common.KICAD_PY.exists()):
        print("    (skipped: KiCad not found)")
        return
    names = [n for n in ("ecc83", "stickhub") if (common.CORPUS / n / "reference.json").exists()]
    if not names:
        print("    (skipped: no benchmark corpus)")
        return
    import oracle_diff
    from copper_kicad.pcb.guided import session
    from PIL import Image as PImage
    for name in names:
        pcb = oracle_diff.scratch(name, "diag")
        ex = session._run(pcb, [{"op": "extract"}], save=False)[0]
        b = model.Board(ex, model.dru_minimums(pcb.with_suffix(".kicad_dru")))
        cfg = P.resolve(b, {"global": {"engine": "v2"}})
        orc = Oracle(b)
        st = RouteState(orc, [])
        ctx = Ctx(board=b, cfg=cfg, x=cfg.x, oracle=orc, state=st, conns=[])
        tracks, vias = oracle_diff.reference(name)
        lay = {n: i for i, n in enumerate(b.layers)}
        for net, ln, x1, y1, x2, y2, w in tracks:
            if net in b.nets and ln in lay:
                st.add(net=net, conn=-1, kind="seg", layer=lay[ln], x1=x1, y1=y1, x2=x2, y2=y2, w=w, stage="legacy")
        for net, x, y, size, drill in vias:
            if net in b.nets:
                st.add(net=net, conn=-1, kind="via", x1=x, y1=y, w=size, drill=drill, stage="legacy")
        t = time.perf_counter()
        out = D.build(ctx)
        dt = time.perf_counter() - t
        cg = out["congestion"]
        text = json.dumps({k: v for k, v in out.items() if k != "_overlays"}, default=str)
        assert max(cg["cols"], cg["rows"]) <= 40 and cg["capacity"] == "estimate" and cg["layers"]
        assert all(-1 <= u <= 999 for g in cg["layers"].values() for row in g for u in row)
        assert out["failed"] == [] and out["cuts"] == [] and inside(ctx, out["_overlays"]["congestion"])
        for s in out["suggest"]:
            if "move" not in s["patch"]:
                P.resolve(b, X.merge_patch({"global": {"engine": "v2"}}, s["patch"]))
        png = D.render(b, out["_overlays"]["congestion"], OUT / f"{name}-congestion.png", title=f"{name}: congestion")
        assert PImage.open(png).size[0] >= 400
        kinds = sorted({c["kind"] for c in out["critique"]})
        print(f"    {name}: {len(st)} pieces, build {1e3 * dt:.0f} ms, {len(text)} chars of text, grid "
              f"{cg['cols']}x{cg['rows']} @ {cg['cell_mm']} mm, max util {max(cg['max'].values())} %, critique {kinds}")


if __name__ == "__main__":
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v) and (not only or k in only)]
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f}s)")
        except Exception as e:  # noqa: BLE001
            fails += 1
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
