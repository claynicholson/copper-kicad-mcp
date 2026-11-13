"""Router v2 engine (design §2, §9): stage pipeline, stage selection, keep-best gate, budget, determinism,
the result contract — and the session hook that writes the judged board.

Pure tests run the real engine / judge / route state / oracle on synthetic boards with scripted stage modules
and a scripted KiCad (``kio.commit_and_check``); two more use the modules that are really built (legacy, detail).
The end-to-end tests go through ``guided.session.route`` on benchmark corpus boards with KiCad 10 (skipped when
KiCad or the corpus is missing).

Run:  python tests/test_v2_engine.py             pure tests + end-to-end on two small corpus boards
      python tests/test_v2_engine.py --pure      pure tests only
      python tests/test_v2_engine.py test_name   one test
Scratch files go to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench" / "route"))
import common  # noqa: E402  (bench/route/common.py)

common.sandbox()       # KICAD_CONFIG_HOME, COPPER_HOME and the temp dir -> bench/route/.work, before copper_kicad loads
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.pcb.guided import model, plan as planmod  # noqa: E402
from copper_kicad.pcb.v2 import engine, judge, kio as real_kio  # noqa: E402
from copper_kicad.pcb.v2.types import STAGE_SHARE, WORK_PER_SECOND, StageReport  # noqa: E402

PURE = "--pure" in sys.argv
_TMP = Path(tempfile.mkdtemp(prefix="engine-", dir=str(common.WORK)))


# ── synthetic boards ──────────────────────────────────────────────────────
def extract(pads, *, size=(40.0, 30.0), zones=()):
    """pads: [(ref, net, x, y)] 0.8 mm square SMD pads on F.Cu, one cluster each."""
    nets = sorted({p[1] for p in pads if p[1]})
    by_net = {}
    for p in pads:
        by_net[p[1]] = by_net.get(p[1], 0) + 1
    ex = {"layers": ["F.Cu", "B.Cu"],
          "rules": {"clearance": 0.15, "track_width": 0.15, "via": 0.6, "drill": 0.3, "edge_clearance": 0.3,
                    "hole_clearance": 0.25, "hole_to_hole": 0.25, "annular": 0.1,
                    "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.2, "dp_width": 0.2}},
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.2, "dp_width": 0.2} for i, n in enumerate(nets)},
          "outline": {"closed": True, "polys": [{"o": [[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [], "vias": [], "zones": [dict(z) for z in zones],
          "mask_openings": [], "unconnected": sum(v - 1 for v in by_net.values()),
          "unconnected_by_net": {n: v - 1 for n, v in by_net.items() if v > 1},
          "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    for i, (ref, net, x, y) in enumerate(pads):
        ex["pads"].append({"ref": ref, "num": "1", "net": net, "x": x, "y": y, "kind": "smd", "layers": [0],
                           "shapes": [[[0], "box", [x, y, 1, 0, 0.4, 0.4, 0.0]]], "c": i + 1})
    return ex


def rows(n, y0=4.0, dy=2.0):
    """n two-pad nets N00.. side by side: left pad at x = 5, right pad at x = 30."""
    pads = []
    for i in range(n):
        pads += [(f"L{i}", f"N{i:02d}", 5.0, y0 + dy * i), (f"R{i}", f"N{i:02d}", 30.0, y0 + dy * i)]
    return pads


def project(name="b"):
    """A project stand-in whose board file exists (the engine reads clearance overrides from its text)."""
    d = Path(tempfile.mkdtemp(prefix="pr-", dir=str(_TMP)))
    (d / f"{name}.kicad_pcb").write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    return types.SimpleNamespace(dir=d, name=name, pcb=d / f"{name}.kicad_pcb")


SCR = types.SimpleNamespace(pcb=None, dir=None, name="scratch")


# ── scripted stage modules and a scripted KiCad ───────────────────────────
class Modules:
    """Replace engine._module: name -> module | None | 'real'."""

    def __init__(self, **mods):
        self.mods, self.real = mods, engine._module

    def __enter__(self):
        real, mods = self.real, self.mods

        def lookup(name):
            v = mods.get(name, None)
            return real(name) if isinstance(v, str) and v == "real" else v
        engine._module = lookup
        return self

    def __exit__(self, *exc):
        engine._module = self.real


def mod(**attrs):
    m = types.ModuleType("scripted")
    m.__dict__.update(attrs)
    return m


def straight(ctx, c, dy=0.0):
    pts = [(c.a.x, c.a.y), (c.b.x, c.b.y)] if not dy else \
        [(c.a.x, c.a.y), (c.a.x + abs(dy), c.a.y + dy), (c.b.x - abs(dy), c.b.y + dy), (c.b.x, c.b.y)]
    if any(ctx.oracle.seg_ok(0, x1, y1, x2, y2, c.width, c.net) is not None for (x1, y1), (x2, y2) in zip(pts, pts[1:])):
        return False
    for r in ctx.cfg.regions:               # plan regions the judge added: a forbidden rectangle is a wall
        if r.forbid and r.applies(c.net):
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                n = max(1, int(math.hypot(x2 - x1, y2 - y1) / 0.1))
                if any(r.rect[0] <= x1 + (x2 - x1) * k / n <= r.rect[2] and r.rect[1] <= y1 + (y2 - y1) * k / n <= r.rect[3]
                       for k in range(n + 1)):
                    return False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        ctx.state.add(net=c.net, conn=c.idx, kind="seg", layer=0, x1=x1, y1=y1, x2=x2, y2=y2, w=c.width, stage="legacy")
    ctx.state.set_status(c.idx, "routed")
    return True


class Legacy:
    """A scripted detail engine with legacy's interface: straight lines (or a rail beside them), COST work units
    per connection, connections the budget does not reach stay 'todo'."""
    COST = 10_000

    def __init__(self, fail=(), rails=(0.0,), only_fails=False):
        self.fail, self.rails, self.calls, self.only_fails = set(fail), rails, [], only_fails

    def route_all(self, ctx, budget, only=None, fair=False):
        self.calls.append((budget.total, None if only is None else list(only)))
        rep = StageReport(name="legacy")
        failed = []
        for c in sorted(ctx.conns, key=lambda c: (c.rank, c.idx)):
            if ctx.state.status(c.idx) == "routed" or (only is not None and c.idx not in only):
                continue
            if budget.spent_out():
                continue
            budget.charge(self.COST)
            if c.net not in self.fail and not (only is not None and self.only_fails)                     and any(straight(ctx, c, d) for d in self.rails):
                rep.routed += 1
            else:
                e = {"net": c.net, "from": c.a.pad, "to": c.b.pad, "reason": "blocked by routed nets: X",
                     "where": [c.a.x, c.a.y], "blocked_by": ["X"], "_a": (c.a.x, c.a.y), "_b": (c.b.x, c.b.y)}
                ctx.state.set_status(c.idx, "failed", e)
                failed.append(e)
        rep.failed = len(failed)
        rep.data = {"failed": failed}
        return rep

    def plan_connections(self, board, cfg, mode="signals", start=0):
        return engine._module_real("legacy").plan_connections(board, cfg, mode, start)


engine._module_real = engine._module


class FakeKicad:
    """Scripted kio.commit_and_check: ``drc(tracks, vias) -> [(type, message, [track index, ...], (x, y))]``;
    unconnected = one per net of ``nets`` that has no track."""

    def __init__(self, drc=None, nets=()):
        self.drc, self.calls, self.nets = drc or (lambda t, v: []), [], list(nets)
        self.marker_pieces = real_kio.marker_pieces

    def commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw):
        n = len(self.calls)
        self.calls.append((tuple(map(tuple, tracks)), tuple(map(tuple, vias))))
        uu = [f"r{n}t{i}" for i in range(len(tracks))] + [f"r{n}v{i}" for i in range(len(vias))]
        new = [{"type": typ, "message": msg, "items": ["Track", "Pad X"], "uuids": [uu[i] for i in mine] + ["pad-x"],
                "pos": [[tracks[i][2], tracks[i][3]] for i in mine] + [list(at)], "at": list(at), "copper": True}
               for typ, msg, mine, at in self.drc(tracks, vias)]
        have = {t[0] for t in tracks}
        unc = {net: 1 for net in self.nets if net not in have}
        length = {}
        for t in tracks:
            e = length.setdefault(t[0], [0.0, 0, 0])
            e[0] += math.hypot(t[4] - t[2], t[5] - t[3])
            e[2] += 1
        return {"uuids": uu, "raw": [{"severity": "error", "type": m["type"]} for m in new], "new": new,
                "unconnected": unc, "stats": {"unconnected_by_net": unc, "net_stats": length,
                                              "totals": {"tracks": len(tracks), "vias": len(vias)}}, "ex": None}

    def extract(self, pr, ripup="none", nets=None):
        raise AssertionError("no pour on these boards: nobody should extract")


def run(pads, plan=None, *, mods, kicad=None, budget=1_000_000, scratch=SCR, x_attrs=None):
    """engine.route on a synthetic board with scripted modules. -> (out, kicad, cfg)"""
    ex = extract(pads)
    b = model.Board(json.loads(json.dumps(ex)))
    p = {"global": {"engine": "v2"}}
    for k, v in (plan or {}).items():
        p[k] = {**p.get(k, {}), **v} if isinstance(v, dict) else v
    cfg = planmod.resolve(b, p)
    for k, v in (x_attrs or {}).items():
        setattr(cfg.x, k, v)
    kicad = kicad or FakeKicad(nets=sorted({q[1] for q in pads}))
    judge.kio = kicad
    mods = dict(mods)
    for name in ("pieces", "planx", "judge", "kio"):
        mods.setdefault(name, "real")
    try:
        with Modules(**mods):
            out = engine.route(project(), cfg, cfg.x, ex, scratch=scratch, baseline_raw=[], budget_units=budget)
    finally:
        judge.kio = real_kio
    return out, kicad, cfg


# ── tests ─────────────────────────────────────────────────────────────────
def test_stages_and_availability():
    assert engine.STAGES == ("classify", "power", "fanout", "global", "detail", "repair", "polish", "judge")
    st = engine.available_stages()
    assert set(st) == set(engine.STAGES) and all(isinstance(v, bool) for v in st.values())
    assert st["judge"] is True                                  # this work package
    assert abs(sum(STAGE_SHARE.values()) - 1.0) < 1e-9 and WORK_PER_SECOND == 200_000


def test_result_has_every_contract_key():
    out, kicad, cfg = run(rows(4), mods={"legacy": Legacy()})
    for k in ("routed", "total", "percent", "failed", "skipped", "nets", "added", "drc", "passes", "plan_used",
              "_board", "_cfg", "_scratch", "stages", "auto", "warnings"):
        assert k in out, k
    assert (out["routed"], out["total"], out["percent"]) == (4, 4, 100.0) and out["failed"] == [] and out["skipped"] == []
    assert out["added"] == {"tracks": 4, "vias": 0, "length_mm": 100.0}
    assert out["drc"] == {"errors": 0, "new_errors": 0, "new_top": []}
    assert out["_judged"] is True and out["_scratch"] is SCR and out["_cfg"] is cfg and out["engine"] == "v2"
    assert out["plan_used"] is cfg.used and isinstance(out["_board"], model.Board)
    assert out["nets"] == {f"N{i:02d}": [25.0, 0] for i in range(4)}
    assert [s["name"] for s in out["stages"]] == ["classify", "legacy", "judge"]
    for s in out["stages"]:
        assert set(s) == {"name", "routed", "failed", "score", "work", "notes"}
        assert set(s["score"]) == {"new_errors", "opens", "necked_mm", "vias", "length_mm", "bends"}
    assert out["stages"][0]["score"]["opens"] == 4 and out["stages"][-1]["score"]["opens"] == 0
    assert [p["pass"] for p in out["passes"]] == ["classify", "legacy", "judge"]
    assert all({"pass", "connections", "routed", "seconds", "work"} <= set(p) for p in out["passes"])
    assert out["judge"]["commits"] == 1 and len(kicad.calls) == 1
    assert isinstance(out["auto"], dict) and out["_targets"] == {f"N{i:02d}" for i in range(4)}
    # the commit list is what KiCad got, in RouteState.export order
    assert out["_commit"] == kicad.calls[0] and len(out["_commit"][0]) == 4


def test_stage_limited_run_stops_before_detail_and_commits_nothing():
    leg = Legacy()
    seen = []
    glob = mod(run=lambda ctx, b, only=None: seen.append("global") or StageReport(name="global", data={"cuts": [{"a": "x"}]}))
    out, kicad, _cfg = run(rows(3), {"global": {"stages": ["classify", "global"]}}, mods={"legacy": leg, "global": glob})
    assert seen == ["global"] and leg.calls == [] and kicad.calls == []       # no search, no commit, no DRC
    assert out["_judged"] is False and out["drc"] is None and out["added"] == {"tracks": 0, "vias": 0, "length_mm": 0.0}
    assert "does not include 'judge'" in out["not_committed"]
    assert [s["name"] for s in out["stages"]] == ["classify", "global"]
    assert out["routed"] == 0 and out["total"] == 3 and out["cuts"] == [{"a": "x"}]
    assert len(out["failed"]) == 3 and all("no search stage ran" in e["reason"] for e in out["failed"])
    # with the search but without the judge: a proposal, still nothing committed
    out, kicad, _cfg = run(rows(3), {"global": {"stages": ["classify", "detail"]}}, mods={"legacy": leg})
    assert kicad.calls == [] and out["_judged"] is False and out["added"]["tracks"] == 3
    assert out["routed"] == 3 and out["failed"] == [] and "not_committed" in out and len(out["_commit"][0]) == 3


def test_same_twice():
    def once():
        out, kicad, _ = run(rows(6, dy=1.2), mods={"legacy": Legacy(rails=(0.0, 0.6))}, budget=45_000)
        return out["_commit"], out["routed"], [(s["name"], s["routed"], s["failed"], s["score"], s["work"]) for s in out["stages"]], \
            [(e["net"], e["reason"]) for e in out["failed"]]
    a, b = once(), once()
    assert a == b and a[1] == 5, a[1:]


def test_no_silent_loss():
    """Every connection ends routed, or in failed[] with a reason — also the ones the budget never reached."""
    out, _k, _c = run(rows(8), mods={"legacy": Legacy(fail={"N01"})}, budget=50_000)
    by_net = {e["net"]: e for e in out["failed"]}
    assert out["total"] == 8 and out["routed"] == 4 and len(out["failed"]) == 4
    assert by_net["N01"]["reason"] == "blocked by routed nets: X" and by_net["N01"]["blocked_by"] == ["X"]
    assert {by_net["N01"]["from"], by_net["N01"]["to"]} == {"L1.1", "R1.1"} and by_net["N01"]["_a"][1] == 6.0
    late = [e for e in out["failed"] if e["net"] != "N01"]
    assert len(late) == 3 and all(e["reason"] == "time budget used up before this connection was tried" for e in late)
    assert out["routed"] + len(out["failed"]) == out["total"]


def test_monotone_in_budget():
    """More budget never returns a worse board (score = new errors, then open connections)."""
    pads = rows(10, dy=1.0)
    prev = None
    for units in (5_000, 20_000, 40_000, 80_000, 200_000, 1_000_000):
        out, _k, _c = run(pads, mods={"legacy": Legacy(rails=(0.0, 0.5))}, budget=units)
        key = (out["drc"]["new_errors"] if out["drc"] else 0, out["total"] - out["routed"])
        assert prev is None or key <= prev, (units, key, prev)
        prev = key
    assert prev == (0, 0)


def test_monotone_in_budget_with_the_built_search():
    """The same with the search that is really built (v1's via legacy; S4 when it is there) on a congested board:
    two columns of pads whose connections all cross the middle."""
    if engine._module("legacy") is None:
        print("  (skipped: legacy is not built)")
        return
    n = 8
    pads = []
    for i in range(n):
        pads += [(f"L{i}", f"N{i:02d}", 6.0, 4.0 + 2.6 * i), (f"R{i}", f"N{i:02d}", 34.0, 4.0 + 2.6 * (n - 1 - i))]
    prev = None
    real = {k: "real" for k in ("legacy", "detail", "repair", "polish", "classify", "window", "search", "tiles")}
    for units in (30_000, 120_000, 600_000, 3_000_000):
        out, _k, _c = run(pads, {"global": {"stages": ["classify", "detail", "repair", "judge"], "grid": 0.2}},
                          mods=real, budget=units)
        key = (out["drc"]["new_errors"] if out["drc"] else 0, out["total"] - out["routed"])
        assert prev is None or key <= prev, (units, key, prev)
        assert out["routed"] + len(out["failed"]) >= out["total"]
        prev = key
    assert prev[1] < n, prev                                    # the big budget routes something


def test_keep_best_gate_undoes_a_stage_that_makes_the_result_worse():
    def bad_polish(ctx, budget):
        c = ctx.conns[0]
        ctx.state.rip_conn(c.idx)
        ctx.state.set_status(c.idx, "failed", {"reason": "polish broke it"})
        budget.charge(77)
        return StageReport(name="polish", notes=["tidied"])
    out, kicad, _c = run(rows(3), mods={"legacy": Legacy(), "polish": mod(run=bad_polish)})
    st = {s["name"]: s for s in out["stages"]}
    assert any("made the result worse" in n and "undone" in n for n in st["polish"]["notes"]), st["polish"]
    assert st["polish"]["score"] == st["legacy"]["score"] and st["polish"]["work"] == 77
    assert out["routed"] == 3 and out["failed"] == [] and len(kicad.calls[0][0]) == 3
    # a stage that improves the score is kept
    def good_polish(ctx, budget):
        p = ctx.state.pieces(conn=0)[0]
        ctx.state.remove(p.id)
        ctx.state.add(net=p.net, conn=0, kind="seg", layer=0, x1=p.x1, y1=p.y1, x2=p.x2, y2=p.y2, w=p.w, stage="polish")
        return StageReport(name="polish")
    out, kicad, _c = run(rows(3), mods={"legacy": Legacy(), "polish": mod(run=good_polish)})
    assert not any("undone" in n for s in out["stages"] for n in s["notes"]) and out["routed"] == 3


class Careless(Legacy):
    """A search that never asks the exact checker: straight lines, whatever is in the way."""

    def route_all(self, ctx, budget, only=None, fair=False):
        rep = StageReport(name="legacy")
        for c in sorted(ctx.conns, key=lambda c: (c.rank, c.idx)):
            if ctx.state.status(c.idx) == "routed" or (only is not None and c.idx not in only):
                continue
            budget.charge(self.COST)
            ctx.state.add(net=c.net, conn=c.idx, kind="seg", layer=0, x1=c.a.x, y1=c.a.y, x2=c.b.x, y2=c.b.y,
                          w=c.width, stage="legacy")
            ctx.state.set_status(c.idx, "routed")
            rep.routed += 1
        return rep


def test_plan_forbidden_areas_are_rules_of_the_exact_checker_and_audited_before_kicad():
    """regions[].forbid / nets[..].avoid are a contract under v2 as under the classic router: the oracle rejects
    copper there, and copper a stage placed without asking never reaches KiCad (so it is never written)."""
    wall = {"regions": [{"rect": [15, 0, 20, 30], "forbid": True, "name": "wall"}]}
    # a search that asks the checker finds no straight line through the wall
    out, kicad, cfg = run(rows(3), wall, mods={"legacy": Legacy()})
    assert out["routed"] == 0 and out["added"]["tracks"] == 0 and len(out["failed"]) == 3, out["failed"]
    assert kicad.calls == [] and out["_judged"] is False
    # one that does not ask: its copper is taken out again before the judge, connection by connection
    out, kicad, cfg = run(rows(3), wall, mods={"legacy": Careless()})
    assert out["routed"] == 0 and out["added"] == {"tracks": 0, "vias": 0, "length_mm": 0.0}, out["added"]
    assert kicad.calls == [] and out["_judged"] is False and out["_commit"] == ((), ())
    assert len(out["failed"]) == 3 and all("plan region 'wall'" in e["reason"] for e in out["failed"]), out["failed"]
    assert any("forbidden area of the plan" in w for w in out["warnings"]), out["warnings"]
    # a region / an avoid rectangle that names nets binds those nets only
    for plan in ({"regions": [{"rect": [15, 0, 20, 30], "forbid": True, "nets": ["N01"], "name": "wall"}]},
                 {"nets": {"N01": {"avoid": [[15, 0, 20, 30]]}}}):
        out, kicad, cfg = run(rows(3), plan, mods={"legacy": Careless()})
        assert out["routed"] == 2 and [e["net"] for e in out["failed"]] == ["N01"], (plan, out["failed"])
        assert sorted(t[0] for t in kicad.calls[-1][0]) == ["N00", "N02"], kicad.calls[-1][0]
    # 'except' lets a net through; a layer-limited region does not bind the other layer
    out, kicad, cfg = run(rows(3), {"regions": [{"rect": [15, 0, 20, 30], "forbid": True, "except": ["N02"]}]},
                          mods={"legacy": Careless()})
    assert [t[0] for t in kicad.calls[-1][0]] == ["N02"] and out["routed"] == 1
    out, kicad, cfg = run(rows(3), {"regions": [{"rect": [15, 0, 20, 30], "forbid": True, "layers": ["B.Cu"]}]},
                          mods={"legacy": Careless()})
    assert out["routed"] == 3 and not out["failed"]


def test_escape_vias_the_routes_did_not_use_are_removed_before_kicad_sees_them():
    """A fan-out via whose pad was then routed on the pad's own layer has copper on one side only: KiCad calls
    it via_dangling. It is removed before the judge; one that is used on the other layer stays."""
    def fan(two_sided):
        def run_(ctx, budget):
            st = ctx.state
            st.add(net="N00", conn=-1, kind="seg", layer=0, x1=5.0, y1=4.0, x2=6.5, y2=4.0, w=0.2, stage="fanout",
                   protected=True)
            st.add(net="N00", conn=-1, kind="via", x1=6.5, y1=4.0, w=0.6, drill=0.3, stage="fanout", protected=True)
            if two_sided:
                st.add(net="N00", conn=-1, kind="seg", layer=1, x1=6.5, y1=4.0, x2=9.0, y2=4.0, w=0.2, stage="fanout",
                       protected=True)
            return StageReport(name="fanout")
        return mod(run=run_)
    full = {"x_attrs": {"classify": "minimal"}}
    out, kicad, _c = run(rows(3), mods={"legacy": Legacy(), "fanout": fan(False)}, **full)
    assert out["routed"] == 3 and out["added"]["vias"] == 0 and kicad.calls[-1][1] == (), (out["added"], kicad.calls[-1][1])
    assert out["added"]["tracks"] == 4                                    # the stub on the pad layer stays
    assert any("escape via(s) the routes did not use were removed" in n for n in out["notes"]), out["notes"]
    out, kicad, _c = run(rows(3), mods={"legacy": Legacy(), "fanout": fan(True)}, **full)
    assert out["routed"] == 3 and out["added"]["vias"] == 1 and len(kicad.calls[-1][1]) == 1, out["added"]
    assert not any("escape via" in n for n in out.get("notes") or [])


def test_reservation_stages_are_gated_on_legality_not_on_score():
    def power_ok(ctx, budget):                                   # adds copper (score "worse": longer) but legal
        ctx.state.add(net="N00", conn=-1, kind="seg", layer=0, x1=5.0, y1=4.0, x2=5.0, y2=2.5, w=0.2, stage="power",
                      protected=True)
        return StageReport(name="power")

    def power_bad(ctx, budget):                                  # a stub of N00 straight through N01's pad
        ctx.state.add(net="N00", conn=-1, kind="seg", layer=0, x1=5.0, y1=4.0, x2=5.0, y2=6.0, w=0.2, stage="power",
                      protected=True)
        return StageReport(name="power")
    full = {"x_attrs": {"classify": "minimal"}}
    out, kicad, _c = run(rows(3), mods={"legacy": Legacy(), "power": mod(run=power_ok)}, **full)
    assert out["added"]["tracks"] == 4 and not any("undone" in n for s in out["stages"] for n in s["notes"])
    out, kicad, _c = run(rows(3), mods={"legacy": Legacy(), "power": mod(run=power_bad)}, **full)
    st = {s["name"]: s for s in out["stages"]}
    assert any("exact checker rejects" in n and "short" in n for n in st["power"]["notes"]), st["power"]["notes"]
    assert out["added"]["tracks"] == 3 and out["routed"] == 3 and len(kicad.calls[0][0]) == 3


def test_a_stage_that_raises_is_undone_and_reported():
    def boom(ctx, budget):
        ctx.state.rip_conn(0)
        raise RuntimeError("polish exploded")
    saved = os.environ.pop("COPPER_V2_STRICT", None)
    try:
        out, _k, _c = run(rows(3), mods={"legacy": Legacy(), "polish": mod(run=boom)})
        assert out["routed"] == 3 and any("polish" in w and "polish exploded" in w for w in out["warnings"]), out["warnings"]
        st = {s["name"]: s for s in out["stages"]}
        assert any("stage failed and was undone" in n for n in st["polish"]["notes"])
        os.environ["COPPER_V2_STRICT"] = "1"                     # tests / benchmark: fail loudly instead
        try:
            run(rows(3), mods={"legacy": Legacy(), "polish": mod(run=boom)})
            raise AssertionError("strict mode must re-raise")
        except RuntimeError as e:
            assert "polish exploded" in str(e)
    finally:
        os.environ.pop("COPPER_V2_STRICT", None)
        if saved is not None:
            os.environ["COPPER_V2_STRICT"] = saved


def test_budget_shares():
    """Work units, never the clock: each stage gets its share; the legacy slot gets everything that is not built."""
    got = {}

    def stage(name, routes=False):
        def fn(ctx, budget, only=None):
            got.setdefault(name, []).append(budget.total)
            if routes:
                for c in ctx.conns:
                    if c.net != "N02" and ctx.state.status(c.idx) != "routed":
                        straight(ctx, c)
                    elif c.net == "N02":
                        ctx.state.set_status(c.idx, "failed", {"reason": "no path"})
            return StageReport(name=name)
        return mod(run=fn)
    B = 1_000_000
    mods = {"power": stage("power"), "fanout": stage("fanout"), "global": stage("global"),
            "detail": stage("detail", routes=True), "repair": stage("repair"), "polish": stage("polish"), "legacy": None}
    out, _k, _c = run(rows(4), mods=mods, budget=B, x_attrs={"classify": "minimal"})
    assert got == {"power": [30_000], "fanout": [50_000], "global": [100_000], "detail": [450_000], "repair": [220_000],
                   "polish": [100_000]}, got
    assert [s["name"] for s in out["stages"]] == ["classify", "power", "fanout", "global", "detail", "repair", "polish", "judge"]
    # stage 1: only the legacy search is built -> it gets the whole budget, as v1's signal pass does
    leg = Legacy()
    run(rows(4), mods={"legacy": leg}, budget=B)
    assert leg.calls == [(B, None)], leg.calls
    # the judge's re-routes get their share or the floor, whichever is larger, even when the search spent everything
    seen = []

    def drc(tracks, vias):
        return [("clearance", "bad", [i], (17.0, 4.0)) for i, t in enumerate(tracks) if t[0] == "N00" and len(seen) < 1]
    leg = Legacy()
    orig = leg.route_all

    def spy(ctx, budget, only=None):
        if only is not None:
            seen.append((budget.total, list(only)))
            return StageReport(name="legacy")
        r = orig(ctx, budget, only)
        budget.charge(budget.left())                             # the search uses everything it was given
        return r
    leg.route_all = spy
    out, _k, _c = run(rows(4), mods={"legacy": leg}, kicad=FakeKicad(drc, nets=[f"N{i:02d}" for i in range(4)]), budget=B)
    assert seen and seen[0][0] == max(int(engine.JUDGE_FLOOR_S * WORK_PER_SECOND), int(STAGE_SHARE["judge"] * B)), seen
    assert seen[0][1] == [0]


def test_judge_removal_is_reported_in_the_result():
    def drc(tracks, vias):
        return [("clearance", "N01 is never right", [i], (17.0, 6.0)) for i, t in enumerate(tracks) if t[0] == "N01"]
    out, kicad, _c = run(rows(3), mods={"legacy": Legacy()}, kicad=FakeKicad(drc, nets=["N00", "N01", "N02"]))
    assert out["routed"] == 2 and out["total"] == 3 and out["drc"]["new_errors"] == 0
    assert [r["net"] for r in out["removed_after_drc"]] == ["N01"] and "KiCad DRC clearance" in out["removed_after_drc"][0]["why"]
    assert len(out["failed"]) == 1 and out["failed"][0]["net"] == "N01"
    assert out["failed"][0]["reason"].startswith("routed, then removed: KiCad DRC clearance: N01 is never right")
    assert all(t[0] != "N01" for t in out["_commit"][0]) and out["added"]["tracks"] == 2
    # the three rounds of the ladder: cost region, forbid region, removal
    assert [r["new_errors"] for r in out["judge"]["rounds"]] == [1, 1, 0]      # cost region, forbid region (no way left), gone


class ApartKicad(FakeKicad):
    """KiCad's ratsnest disagrees with the router's model: ``apart`` stays unconnected whatever copper it has (the
    extract of the judged board says which pads: one connectivity cluster per pad)."""

    def __init__(self, apart, pads, with_ex=True, **kw):
        super().__init__(**kw)
        self.apart, self.pads, self.with_ex = apart, pads, with_ex

    def commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw):
        res = super().commit_and_check(pr, scratch, tracks, vias, baseline_raw, **kw)
        res["unconnected"] = {**res["unconnected"], self.apart: 1}
        res["stats"]["unconnected_by_net"] = res["unconnected"]
        if self.with_ex:
            ex = extract(self.pads)
            joined = {t[0] for t in tracks} - {self.apart}
            first = {}
            for q in ex["pads"]:
                if q["net"] in joined:
                    q["c"] = first.setdefault(q["net"], q["c"])
            res["ex"] = ex
        return res


def test_an_open_net_the_model_calls_routed_is_listed_in_failed():
    """No silent loss, the other way round: the router's model has every connection laid, KiCad's ratsnest of the
    judged board still shows a net open (the independent verifier's run: 23/24 with failed []). The count is
    KiCad's, and failed[] names that net and the pads KiCad holds apart — with the judged extract at hand, and
    from the net's connection without it."""
    pads = rows(3)
    for with_ex in (True, False):
        out, _k, _c = run(pads, mods={"legacy": Legacy()},
                          kicad=ApartKicad("N01", pads, with_ex=with_ex, nets=["N00", "N01", "N02"]))
        assert out["total"] == 3 and out["routed"] == 2, (with_ex, out["routed"])
        assert [e["net"] for e in out["failed"]] == ["N01"], (with_ex, out["failed"])
        e = out["failed"][0]
        assert {e["from"], e["to"]} == {"L1.1", "R1.1"} and e["where"] and e["_a"] != e["_b"], (with_ex, e)
        assert "KiCad still shows 1 open connection(s)" in e["reason"] and "ripup: 'nets'" in e["reason"], e["reason"]
        assert out["routed"] + len(out["failed"]) == out["total"]
    # a net KiCad shows connected gets no such record, and a failure the router knows keeps its own reason
    out, _k, _c = run(pads, mods={"legacy": Legacy(fail={"N01"})}, kicad=FakeKicad(nets=["N00", "N01", "N02"]))
    assert [e["reason"] for e in out["failed"]] == ["blocked by routed nets: X"]
    out, _k, _c = run(pads, mods={"legacy": Legacy()})
    assert out["routed"] == out["total"] == 3 and out["failed"] == []


def test_stage1_wide_reroute_is_kept_only_when_it_helps():
    """Build stage 1: the legacy search cannot move fixed copper, so a local re-route of what KiCad rejected can
    fail where a route from scratch (with the judge's regions in place) succeeds. Bounded, and under keep-best."""
    def drc(tracks, vias):
        return [("clearance", "N01 too close to something KiCad sees", [i], (17.0, 6.0))
                for i, t in enumerate(tracks) if t[0] == "N01" and abs(t[3] - 6.0) < 1e-6 and abs(t[5] - 6.0) < 1e-6]
    nets = ["N00", "N01", "N02"]
    leg = Legacy(rails=(0.0, 0.6), only_fails=True)
    out, kicad, _c = run(rows(3), mods={"legacy": leg}, kicad=FakeKicad(drc, nets=nets))
    assert out["routed"] == 3 and out["failed"] == [] and out["drc"]["new_errors"] == 0, (out["routed"], out["failed"])
    assert [c[1] for c in leg.calls] == [None, [1], None, [1], None], leg.calls      # local, wide, local, wide
    assert any("routed again from scratch" in n for n in out["notes"]), out.get("notes")
    assert "removed_after_drc" not in out
    # a board over the size limit is not restarted: the connection is removed and reported instead
    saved, engine.WIDE_MAX_CONNS = engine.WIDE_MAX_CONNS, 2
    try:
        leg = Legacy(rails=(0.0, 0.6), only_fails=True)
        out, kicad, _c = run(rows(3), mods={"legacy": leg}, kicad=FakeKicad(drc, nets=nets))
    finally:
        engine.WIDE_MAX_CONNS = saved
    assert [c[1] for c in leg.calls] == [None, [1]] and out["routed"] == 2 and out["removed_after_drc"][0]["net"] == "N01"
    # a restart that does not improve the score is undone (same pieces as before it)
    leg = Legacy(rails=(0.0,), only_fails=True)                  # no other rail: from scratch N01 lands where it was
    out, kicad, _c = run(rows(3), mods={"legacy": leg}, kicad=FakeKicad(drc, nets=nets))
    assert out["routed"] == 2 and out["drc"]["new_errors"] == 0 and len(out["failed"]) == 1


def test_classify_selection_and_detail_engine():
    """S0 as designed comes in with the power stage; build stage 1 uses v1's connection list. The window detail
    is used when it is built, the legacy search stays selectable."""
    marks = []

    def full(ctx, budget):
        marks.append("full")
        ctx.conns = engine.minimal_connections(ctx.board, ctx.cfg)
        return StageReport(name="classify", data={"skipped": [{"net": "Z", "connections": 1, "reason": "plan: skip"}]})
    cl = mod(run=full)
    power = mod(run=lambda ctx, b: StageReport(name="power"))
    out, _k, _c = run(rows(2), mods={"legacy": Legacy(), "classify": cl})
    assert marks == [] and "minimal classify" in out["stages"][0]["notes"][0]             # power is not built
    out, _k, _c = run(rows(2), mods={"legacy": Legacy(), "classify": cl, "power": power})
    assert marks == ["full"] and out["skipped"] == [{"net": "Z", "connections": 1, "reason": "plan: skip"}]
    out, _k, _c = run(rows(2), {"global": {"stages": ["classify", "detail", "judge"]}},
                      mods={"legacy": Legacy(), "classify": cl, "power": power})
    assert marks == ["full"]                                                               # power not selected
    out, _k, _c = run(rows(2), mods={"legacy": Legacy(), "classify": cl}, x_attrs={"classify": "full"})
    assert marks == ["full", "full"]
    # detail engine
    det_calls = []

    def det(ctx, budget, only=None):
        det_calls.append(only)
        for c in ctx.conns:
            if ctx.state.status(c.idx) != "routed" and c.net != "N01":
                straight(ctx, c)
        return StageReport(name="detail")
    leg = Legacy()
    out, _k, _c = run(rows(3), mods={"legacy": leg, "detail": mod(run=det)})
    assert det_calls == [None] and [s["name"] for s in out["stages"]] == ["classify", "detail", "legacy", "judge"]
    # legacy = fallback per connection, with everything the search budget still holds except what polish and the
    # judge's re-routes are promised (build stage 3)
    rest = 1_000_000 - int((STAGE_SHARE["polish"] + STAGE_SHARE["judge"]) * 1_000_000)
    assert leg.calls == [(rest, [1])] and out["routed"] == 3
    leg = Legacy()
    out, _k, _c = run(rows(3), mods={"legacy": leg, "detail": mod(run=det)}, x_attrs={"detail": "legacy"})
    assert det_calls == [None] and leg.calls == [(1_000_000, None)]
    assert [s["name"] for s in out["stages"]] == ["classify", "legacy", "judge"]


def _fill_zone(net, rect):
    x0, y0, x1, y1 = rect
    o = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return {"name": net.lower(), "layers": [0], "net": net, "priority": 0, "filled": True, "polys": [{"o": o}],
            "fills": {"0": [{"o": o}]}}


def _local_fill_board():
    """P: poured, but the fill is a patch round P1 (4 pads in 4 pieces). G: poured, the fill joins G1..G3 (KiCad's
    connectivity: one cluster) and misses G4."""
    pads = rows(1) + [("P1", "P", 5.0, 16.0), ("P2", "P", 15.0, 16.0), ("P3", "P", 25.0, 16.0), ("P4", "P", 35.0, 16.0),
                      ("G1", "G", 5.0, 24.0), ("G2", "G", 15.0, 24.0), ("G3", "G", 25.0, 24.0), ("G4", "G", 35.0, 24.0)]
    ex = extract(pads, zones=[_fill_zone("P", (3.0, 14.0, 8.0, 18.0)), _fill_zone("G", (3.0, 22.0, 27.0, 26.0))])
    first = next(q["c"] for q in ex["pads"] if q["ref"] == "G1")
    for q in ex["pads"]:
        if q["ref"] in ("G2", "G3"):
            q["c"] = first
    return pads, ex


def test_local_fill_net_is_planned_with_the_signals():
    """A poured net whose pour joins few of its pads (a local fill on a signal layer) gets an MST over its pad
    clusters when the run asks for it (the second opinion does); a net whose pour joins most pads does not."""
    _pads, ex = _local_fill_board()
    b = model.Board(json.loads(json.dumps(ex)))
    cfg = planmod.resolve(b, {"global": {"engine": "v2"}})
    assert b.nets["P"].has_pour and b.nets["G"].has_pour
    assert engine.local_fill_nets(b, cfg) == ["P"]
    plain = engine.minimal_connections(b, cfg)
    assert sorted({c.net for c in plain}) == ["N00"]                       # v1's plan: poured nets go to the pours pass
    lf = engine.minimal_connections(b, cfg, local_fill=True)
    pc = [c for c in lf if c.net == "P"]
    assert len(pc) == 3 and all(c.a.pad and c.b.pad and c.a.cluster != c.b.cluster for c in pc)
    assert not any(c.net == "G" for c in lf) and [c.idx for c in lf] == list(range(len(lf)))
    assert b.nets["P"].has_pour                                            # the planner's flag is put back
    # a pour on a layer the plan keeps signals off is a plane: a via per pad is the connection, not a track
    cfg2 = planmod.resolve(b, {"global": {"engine": "v2", "layers": ["B.Cu"]}})
    assert engine.local_fill_nets(b, cfg2) == []


def test_bridge_spec_between_the_two_parts_of_a_tree():
    """engine._bridge_spec(to=cluster): the sides of a pad-to-pad bridge are everything joined to terminal a and
    everything joined to terminal b — not the rest of the net."""
    from copper_kicad.pcb.v2 import pieces
    from copper_kicad.pcb.v2.oracle import Oracle
    from copper_kicad.pcb.v2.types import ConnV2, Terminal
    pads = [("A1", "A", 5.0, 5.0), ("A2", "A", 10.0, 5.0), ("A3", "A", 20.0, 5.0), ("A4", "A", 25.0, 5.0),
            ("A5", "A", 35.0, 25.0)]
    b = model.Board(json.loads(json.dumps(extract(pads))))
    by = {q.name: q for q in b.pads}

    def term(n):
        q = by[n + ".1"]
        return Terminal(q.x, q.y, tuple(q.layers), pad=q.name, cluster=q.cluster)

    def cn(i, a, c):
        return ConnV2(idx=i, net="A", a=term(a), b=term(c), est=5.0, width=0.2, neck=0.2, clearance=0.2, via=(0.6, 0.3),
                      rank=(i,), tree_target=True)
    conns = [cn(0, "A1", "A2"), cn(1, "A3", "A4"), cn(2, "A2", "A3")]
    st = pieces.RouteState(Oracle(b), conns)
    for i, (x1, x2) in ((0, (5.0, 10.0)), (1, (20.0, 25.0))):
        st.add(net="A", conn=i, kind="seg", layer=0, x1=x1, y1=5.0, x2=x2, y2=5.0, w=0.2, stage="legacy")
        st.set_status(i, "routed")
    spec = engine._bridge_spec(b, st, conns[2], to=conns[2].b.cluster)
    assert sorted(t.pad for t in spec["src"]["pads"]) == ["A1.1", "A2.1"]
    assert sorted(t.pad for t in spec["tgt"]["pads"]) == ["A3.1", "A4.1"]              # A5 is not this bridge's business
    assert len(spec["src"]["segs"]) == 1 and len(spec["tgt"]["segs"]) == 1
    assert spec["box"] == (10.0, 5.0, 20.0, 5.0) and spec["gap"] == 10.0
    allnet = engine._bridge_spec(b, st, conns[2])                                     # the pours pass's form: the rest
    assert sorted(t.pad for t in allnet["tgt"]["pads"]) == ["A3.1", "A4.1", "A5.1"]
    st.add(net="A", conn=2, kind="seg", layer=0, x1=10.0, y1=5.0, x2=20.0, y2=5.0, w=0.2, stage="detail")
    assert engine._bridge_spec(b, st, conns[2], to=conns[2].b.cluster) is None        # one part already


def test_second_opinion_keeps_the_better_judged_result():
    """Build stage 3: when the staged pipeline (window search first) leaves connections open, build stage 1's
    pipeline routes the board again from scratch and the result KiCad judges better is the one returned. An
    explicit stage / detail choice in the plan, or COPPER_V2_ALT=0, turns the second opinion off."""
    def det(ctx, budget, only=None):                     # the window search cannot do N01
        for c in ctx.conns:
            if ctx.state.status(c.idx) != "routed" and c.net != "N01" and (only is None or c.idx in only):
                straight(ctx, c)
        return StageReport(name="detail")

    def scratch():
        d = Path(tempfile.mkdtemp(prefix="scr-", dir=str(_TMP)))
        f = d / "scratch.kicad_pcb"
        f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
        return types.SimpleNamespace(pcb=f, dir=d, name="scratch")
    nets = ["N00", "N01", "N02"]
    # v1's search routes everything when it has the whole board, nothing as a per-connection fallback
    leg, scr = Legacy(only_fails=True), scratch()
    out, kicad, cfg = run(rows(3), mods={"legacy": leg, "detail": mod(run=det)}, kicad=FakeKicad(nets=nets), scratch=scr)
    assert out["routed"] == 3 and out["total"] == 3 and out["drc"]["new_errors"] == 0
    assert [(r["open"], r["kept"]) for r in out["portfolio"]] == [(1, False), (0, True)]
    assert [c[1] for c in leg.calls] == [[1], None]      # fallback for N01, then the whole board
    assert any(p["pass"].startswith("first attempt (not kept)") for p in out["passes"])
    assert not list(scr.dir.glob("*.v2pre")) and not list(scr.dir.glob("*.v2alt"))           # helper files are gone
    assert not [r for r in cfg.regions if r.name == "judge"]
    # the second opinion is not better: the first result stands, untouched
    leg, scr = Legacy(fail={"N01", "N02"}), scratch()
    out, kicad, cfg = run(rows(3), mods={"legacy": leg, "detail": mod(run=det)}, kicad=FakeKicad(nets=nets), scratch=scr)
    assert out["routed"] == 2 and [(r["open"], r["kept"]) for r in out["portfolio"]][0] == (1, True)
    assert [s["name"] for s in out["stages"]][:2] == ["classify", "detail"]
    # complete at the first attempt: no second run
    leg, scr = Legacy(), scratch()
    out, kicad, cfg = run(rows(3), mods={"legacy": leg, "detail": mod(run=det)}, kicad=FakeKicad(nets=nets), scratch=scr)
    assert out["routed"] == 3 and "portfolio" not in out and [c[1] for c in leg.calls] == [[1]]
    # switched off
    os.environ["COPPER_V2_ALT"] = "0"
    try:
        leg, scr = Legacy(only_fails=True), scratch()
        out, kicad, cfg = run(rows(3), mods={"legacy": leg, "detail": mod(run=det)}, kicad=FakeKicad(nets=nets),
                              scratch=scr)
        assert out["routed"] == 2 and "portfolio" not in out
    finally:
        os.environ.pop("COPPER_V2_ALT", None)
    leg, scr = Legacy(only_fails=True), scratch()
    out, kicad, cfg = run(rows(3), {"global": {"stages": ["classify", "detail", "judge"]}},
                          mods={"legacy": leg, "detail": mod(run=det)}, kicad=FakeKicad(nets=nets), scratch=scr)
    assert out["routed"] == 2 and "portfolio" not in out             # the plan chose its stages


def test_via_frugal_run_is_kept_only_on_an_easy_board():
    """Stage 6: the staged pipeline first runs at VIA_FRUGAL x the plan's via cost. Its result is returned when
    the frugal detail stage fails next to nothing and the run ends complete and clean; otherwise the normal run
    routes the board from scratch with the plan's costs. The plan's costs are back in place either way."""
    seen = []

    def glob(ctx, budget, only=None):
        return StageReport(name="global")

    def det(hard):
        def run_(ctx, budget, only=None, max_failed=None):
            seen.append((ctx.cfg.via_cost, max_failed))
            for c in ctx.conns:
                if ctx.state.status(c.idx) != "routed" and (only is None or c.idx in only):
                    straight(ctx, c)
            rep = StageReport(name="detail")
            rep.data = {"hard_failed": hard if max_failed is not None else 0}
            return rep
        return run_

    def scratch():
        d = Path(tempfile.mkdtemp(prefix="scr-", dir=str(_TMP)))
        f = d / "scratch.kicad_pcb"
        f.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
        return types.SimpleNamespace(pcb=f, dir=d, name="scratch")
    nets = ["N00", "N01", "N02"]
    ks = list(engine.VIA_FRUGAL_LADDER)      # every multiple is tried; the candidate with the fewest vias is kept
    assert len(ks) >= 1 and all(k > 1.0 for k in ks)
    out, kicad, cfg = run(rows(3), mods={"global": mod(run=glob), "detail": mod(run=det(0))},
                          kicad=FakeKicad(nets=nets), scratch=scratch())
    assert out["routed"] == 3 and any("easy board" in n for n in out["notes"]), out["notes"]
    assert [v for v, _m in seen] == [5.0 * k for k in ks] and seen[0][1] == engine.VIA_FRUGAL_FAILS_MIN
    assert Path(out["_scratch"].pcb).exists() and not list(Path(out["_scratch"].pcb).parent.glob("*.v2frugal"))
    assert cfg.via_cost == 5.0 and all(nc.via_cost == 5.0 for nc in cfg.nets.values())
    # not easy: the frugal attempt is dropped, the normal run's result is returned
    del seen[:]
    out, kicad, cfg = run(rows(3), mods={"global": mod(run=glob), "detail": mod(run=det(9))},
                          kicad=FakeKicad(nets=nets), scratch=scratch())
    assert out["routed"] == 3 and not any("easy board" in n for n in out["notes"])
    assert [v for v, _m in seen] == [5.0 * ks[0], 5.0] and seen[-1][1] is None      # one trial, then the normal run
    assert cfg.via_cost == 5.0 and not [r for r in cfg.regions if r.name == "judge"]
    # switched off
    del seen[:]
    os.environ["COPPER_V2_FRUGAL"] = "0"
    try:
        out, kicad, cfg = run(rows(3), mods={"global": mod(run=glob), "detail": mod(run=det(0))},
                              kicad=FakeKicad(nets=nets), scratch=scratch())
    finally:
        os.environ.pop("COPPER_V2_FRUGAL", None)
    assert [v for v, _m in seen] == [5.0] and out["routed"] == 3


def test_missing_modules_are_named_not_crashed():
    try:
        run(rows(2), mods={"pieces": None, "legacy": Legacy()})
        raise AssertionError("expected V2_UNAVAILABLE")
    except ToolError as e:
        assert e.code == "V2_UNAVAILABLE"
    out, kicad, _c = run(rows(2), mods={"legacy": None})
    assert out["routed"] == 0 and kicad.calls == [] and any("no detail search" in w for w in out["warnings"])
    assert len(out["failed"]) == 2 and out["_judged"] is False


# ── electrical constraints (design §14.6, §14.10) ─────────────────────────
class ConsKicad(FakeKicad):
    """FakeKicad whose raw list is shaped like kicad-cli's (type, description, item texts with the net)."""

    def commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw):
        res = FakeKicad.commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw)
        raw = []
        for (typ, msg, mine, _at), m in zip(self.drc(tracks, vias), res["new"]):
            items = [f"Track [{tracks[i][0]}] on F.Cu, length 25.0000 mm" for i in mine]
            m["items"] = items
            raw.append({"type": typ, "severity": "error", "description": msg, "items": [{"description": t} for t in items]})
        res["raw"] = raw
        return res


def test_constraint_markers_cost_no_connection_and_are_reported_with_the_amount():
    """KiCad flags two nets as too short and one as badly matched. Nothing is ripped up or removed; the result
    carries KiCad's count (drc.constraint_errors, constraints.kicad) and the group's shortfall in mm."""
    def drc(tracks, vias):
        out = []
        for i, t in enumerate(tracks):
            if t[0] in ("N00", "N01"):
                out.append(("length_out_of_range", "Track length out of range (rule 'bus' min length 39.5000 mm; "
                            "actual 25.0000 mm)", [i], (17.0, t[3])))
            elif t[0] == "N02":
                out.append(("skew_out_of_range", "Skew between tracks out of range (rule 'm' max skew 0.1000 mm; "
                            "actual -1.0000 mm; target net length 26.0000 mm (from N09); actual 25.0000 mm)", [i], (17.0, t[3])))
        return out
    plan = {"groups": [{"nets": ["N00", "N01"], "tol": 0.5, "len": 40.0, "name": "bus"}]}
    leg = Legacy()
    out, kicad, cfg = run(rows(3), plan, mods={"legacy": leg, "constraints": "real"},
                          kicad=ConsKicad(drc, nets=["N00", "N01", "N02"]))
    assert (out["routed"], out["total"]) == (3, 3) and out["failed"] == [] and "removed_after_drc" not in out
    assert len(kicad.calls) == 1 and out["judge"]["commits"] == 1 and len(leg.calls) == 1       # no re-route, no 2nd pipeline
    assert [r["new_errors"] for r in out["judge"]["rounds"]] == [0]
    assert out["drc"] == {"errors": 3, "new_errors": 3, "new_top": [], "constraint_errors": 3}
    assert engine._key(out) == (0, 0)                                    # complete and clean as far as copper goes
    c = out["constraints"]
    assert c["mode"] == "auto" and c["kicad"] == {"length_out_of_range": 2, "skew_out_of_range": 1}
    g = c["groups"][0]
    assert (g["name"], g["n"], g["src"], g["target"], g["tol"], g["ok"]) == ("bus", 2, "plan", 40.0, 0.5, False)
    assert g["min"] == g["max"] == 25.0 and g["short_by"] == 14.5 and g["dev"] == -15.0
    assert (c["ok"], c["missed"]) == (0, 1)
    assert c["kicad_other"] == [["skew_out_of_range", "N02"]]            # an error no row of the report explains
    assert c["nets"] == {"N00": [25.0, 25.0, 0.0, 0.0, 0], "N01": [25.0, 25.0, 0.0, 0.0, 0]} and c["len_model_err"] == 0.0
    pv = out["auto"]["constraints"]
    assert pv["groups"] == [{"name": "bus", "nets": ["N00", "N01"], "tol": 0.5, "mode": "tune", "src": "plan", "conf": 1.0,
                             "why": "plan.groups[0]", "len": 40.0}]
    assert cfg.x.cons.tuned()[0].name == "bus" and not out["warnings"]


def test_a_board_without_constraints_has_no_constraints_block():
    out, kicad, cfg = run(rows(3), mods={"legacy": Legacy(), "constraints": "real"})
    assert "constraints" not in out and "constraints" not in out["auto"]
    assert out["drc"] == {"errors": 0, "new_errors": 0, "new_top": []} and not out["warnings"]
    assert cfg.x.cons is not None and not cfg.x.cons.any()
    # global.constraints = "off": nothing resolved at all, even with groups in the plan
    out, _k, cfg = run(rows(3), {"global": {"constraints": "off"}, "groups": [{"nets": ["N00", "N01"], "tol": 0.5}]},
                       mods={"legacy": Legacy(), "constraints": "real"})
    assert "constraints" not in out and cfg.x.cons.mode == "off" and not cfg.x.cons.any()
    # "report": measured like "auto", but no group may get copper
    out, _k, cfg = run(rows(3), {"global": {"constraints": "report"}, "groups": [{"nets": ["N00", "N01"], "tol": 0.5}]},
                       mods={"legacy": Legacy(), "constraints": "real"})
    assert out["constraints"]["groups"][0]["ok"] is True and cfg.x.cons.tuned() == ()
    # the same copper with and without the constraint fields (this build measures, it does not tune)
    plain, _k, _c = run(rows(3), mods={"legacy": Legacy()})
    assert out["_commit"] == plain["_commit"]


# ── end to end: session.route on corpus boards (KiCad 10) ─────────────────
def have_kicad() -> bool:
    return common.KICAD_CLI.exists() and common.KICAD_PY.exists() and (common.CORPUS / "ecc83" / "ecc83.kicad_pcb").exists()


def corpus_project(board: str, tag: str):
    from copper_kicad import project as projmod
    work = _TMP / f"{board}-{tag}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for f in (common.CORPUS / board).iterdir():
        if f.is_file() and f.name not in ("meta.json", "reference.json"):
            shutil.copy2(f, work / f.name)
    return projmod.Project(work, board)


def engine_commit(board: str, tag: str, stages=None):
    """The engine on a corpus board exactly as the session hook calls it. -> (commit list, out)"""
    from copper_kicad.pcb import guided
    from copper_kicad.pcb.guided import router as routermod, session
    pr = corpus_project(board, tag)
    plan = guided.analyze(pr, ripup="none")["suggested_plan"]
    plan.setdefault("global", {})["engine"] = "v2"
    if stages:
        plan["global"]["stages"] = stages
    b, ex, _before, _r = session.load_board(pr, "none", with_before=True)
    cfg = planmod.resolve(b, plan)
    sc = session._scratch(pr)
    try:
        out = engine.route(pr, cfg, cfg.x, ex, scratch=sc, baseline_raw=session._raw_drc(sc),
                           budget_units=int(cfg.time_budget * routermod.WORK_PER_SECOND))
        return out["_commit"], out
    finally:
        shutil.rmtree(sc.dir, ignore_errors=True)


STAGE1 = ["classify", "detail", "judge"]


class env:
    """Set environment variables for the length of a with block."""

    def __init__(self, **kv):
        self.kv, self.saved = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_e2e_same_twice_on_two_corpus_boards():
    """Build stage 1 (v1 connections → legacy search → judge) on two boards the baseline routes completely:
    identical commit lists on a second run, and still complete and clean."""
    if PURE or not have_kicad():
        print("  (skipped: needs KiCad 10 and the benchmark corpus)")
        return
    with env(COPPER_V2_DETAIL="legacy", COPPER_V2_STRICT="1"):
        for board in ("ecc83", "sonde_xilinx"):
            c1, o1 = engine_commit(board, "a", STAGE1)
            c2, o2 = engine_commit(board, "b", STAGE1)
            assert c1 == c2 and len(c1[0]) > 10, board              # identical commit lists
            assert (o1["routed"], o1["total"], o1["drc"]) == (o2["routed"], o2["total"], o2["drc"])
            assert [s["name"] for s in o1["stages"]][:3] == ["classify", "legacy", "judge"], o1["stages"]
            assert o1["_judged"] and o1["drc"]["new_errors"] == 0 and o1["routed"] == o1["total"],                 (board, o1["routed"], o1["total"])


def test_e2e_same_twice_with_every_built_stage():
    """The default pipeline (every stage that is built today): deterministic, nothing lost silently, no new DRC
    error shipped. How much it routes is the business of the stages and of the benchmark, not of this test."""
    if PURE or not have_kicad():
        print("  (skipped: needs KiCad 10 and the benchmark corpus)")
        return
    c1, o1 = engine_commit("sonde_xilinx", "a")
    c2, o2 = engine_commit("sonde_xilinx", "b")
    assert c1 == c2 and len(c1[0]) > 10
    assert (o1["routed"], o1["total"], o1["drc"]) == (o2["routed"], o2["total"], o2["drc"])
    assert [(s["name"], s["routed"], s["failed"], s["score"]) for s in o1["stages"]] ==         [(s["name"], s["routed"], s["failed"], s["score"]) for s in o2["stages"]]
    assert o1["_judged"] and o1["drc"]["new_errors"] == 0
    assert o1["routed"] + len(o1["failed"]) >= o1["total"], (o1["routed"], len(o1["failed"]), o1["total"])
    print("   stages:", [(s["name"], s["routed"], s["failed"]) for s in o1["stages"]], "->", o1["routed"], "/", o1["total"],
          "warnings:", o1["warnings"])


def test_e2e_session_dry_run_plan_only_and_write():
    """Through guided.session.route (build stage 1's pipeline): a stage-limited dry run and a full dry run leave
    the board file untouched; a real run replaces it once, with a checkpoint, by a board KiCad finds complete and
    clean; compare / rip-up / nets=[...] behave as with the v1 engine."""
    if PURE or not have_kicad():
        print("  (skipped: needs KiCad 10 and the benchmark corpus)")
        return
    from copper_kicad.pcb import guided
    pr = corpus_project("ecc83", "session")
    before = pr.pcb.read_bytes()
    plan = guided.analyze(pr, ripup="none")["suggested_plan"]
    plan.setdefault("global", {}).update(engine="v2", stages=STAGE1)
    with env(COPPER_V2_DETAIL="legacy", COPPER_V2_STRICT="1"):
        # 1. stage-limited dry run: stops before detail, nothing is written, no scratch board is handed back
        lim = json.loads(json.dumps(plan))
        lim["global"]["stages"] = ["classify", "power", "fanout", "global"]
        out = guided.route(pr, lim, dry_run=True)
        assert pr.pcb.read_bytes() == before and out["written"] is False and out.get("dry_run") is True
        assert "_scratch" not in out and out["added"]["tracks"] == 0 and "not_committed" in out
        assert not any(s["name"] in ("detail", "legacy", "judge") for s in out["stages"]), [s["name"] for s in out["stages"]]
        assert out["total"] == 14 and len(out["failed"]) == 14 and "drc" not in out
        # ... and without dry_run the same plan still writes nothing, and says so
        out = guided.route(pr, lim)
        assert pr.pcb.read_bytes() == before and out["written"] is False and "not_written" in out and out["routed"] == 0
        # 2. full dry run: judged on a scratch copy, board untouched
        out = guided.route(pr, plan, dry_run=True)
        sc = out.pop("_scratch")
        try:
            assert pr.pcb.read_bytes() == before and out["written"] is False and out["dry_run"] is True
            assert out["routed"] == out["total"] == 14 and out["drc"]["errors"] == 0 and out["drc"]["new_errors"] == 0
            assert sc.pcb.exists() and sc.pcb.read_bytes() != before
            d = common.drc(sc.pcb)
            assert d["errors"] == 0 and d["unconnected"] == 0, d
        finally:
            shutil.rmtree(sc.dir, ignore_errors=True)
        # 3. the real run
        out = guided.route(pr, plan)
        assert out["written"] is True and out["checkpoint"] and pr.pcb.read_bytes() != before
        assert out["routed"] == out["total"] == 14 and out["percent"] == 100.0 and out["unrouted_after"] == 0
        assert out["drc"]["errors"] == 0 and out["drc"]["new_errors"] == 0 and out["engine"] == "v2"
        assert "_scratch" not in out and out["added"]["tracks"] > 10 and out["nets"] and "previous" not in out
        for k in ("routed", "total", "percent", "failed", "skipped", "nets", "added", "drc", "passes", "plan_used", "_board",
                  "_cfg", "stages", "auto", "unrouted_before", "unrouted_after", "totals", "seconds", "revision", "warnings"):
            assert k in out, k
        assert not [k for k in out if k.startswith("_") and k not in ("_board", "_cfg")], [k for k in out if k.startswith("_")]
        d = common.drc(pr.pcb)
        assert d["errors"] == 0 and d["unconnected"] == 0, d
        assert not list(pr.dir.glob("*.copper-tmp")) and not list(pr.dir.glob("*.v2base"))
        tracks = out["totals"]["tracks"]
        # 4. nothing left to do: nothing written
        routed = pr.pcb.read_bytes()
        out = guided.route(pr, plan)
        assert out["written"] is False and pr.pcb.read_bytes() == routed and out.get("nothing_to_route") is True
        # 5. compare: re-route everything on a scratch copy and score both, board untouched
        out = guided.route(pr, plan, compare=True)
        sc = out.pop("_scratch", None)
        if sc is not None:
            shutil.rmtree(sc.dir, ignore_errors=True)
        assert pr.pcb.read_bytes() == routed and out["written"] is False and out["dry_run"] is True
        cmp_ = out["compare"]
        assert cmp_["better"] in ("tie", "guided", "existing") and cmp_["existing"]["unrouted"] == 0
        assert cmp_["guided"]["unrouted"] == 0 and cmp_["guided"]["drc_errors"] == 0 and out["ripped_up"] == tracks
        # 6. rip up one net and route it again: only that net is targeted
        net = sorted(n for n, v in out["nets"].items() if v[0] > 0)[0]
        out = guided.route(pr, plan, ripup="nets", nets=[net])
        assert out["written"] is True and out["ripped_up"] >= 1 and 1 <= out["total"] <= 3 and out["routed"] == out["total"]
        assert out["unrouted_after"] == 0 and out["drc"]["new_errors"] == 0 and list(out["nets"]) == [net]
        assert out["previous"]["unrouted"] == 0 and "checkpoint(action='restore'" in out["previous"]["restore"]
        d = common.drc(pr.pcb)
        assert d["errors"] == 0 and d["unconnected"] == 0, d
        # 7. rip up everything and route again (deterministic: the same copper as the first time)
        out = guided.route(pr, plan, ripup="all")
        assert out["written"] is True and out["routed"] == out["total"] == 14 and out["totals"]["tracks"] == tracks
        d = common.drc(pr.pcb)
        assert d["errors"] == 0 and d["unconnected"] == 0, d


if __name__ == "__main__":
    import time
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    try:
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
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
