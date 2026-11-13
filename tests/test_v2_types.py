"""Router v2 shared types (design §3): q(), Score ordering, Budget arithmetic, Piece / Ctx shape.

Run:  python tests/test_v2_types.py      (pure Python, no KiCad)
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad.pcb.v2 import types as T  # noqa: E402


def test_q_is_idempotent_and_1nm():
    for v in (0.0, 1.0, -3.25, 126.509999999, 81.4200000004, 0.1 + 0.2, 1e-7, -1e-7, 123456.7890123456, 2 ** 0.5):
        a = T.q(v)
        assert T.q(a) == a, v
        assert abs(a - v) <= 0.5e-6 + 1e-12, v
        assert abs(a * 1e6 - round(a * 1e6)) < 1e-6, v          # a whole number of nanometres
    assert T.q(0.1 + 0.2) == 0.3
    assert T.q(10.6000000004) == T.q(10.6) == 10.6
    assert T.q(0.0000004) == 0.0 and T.q(0.0000006) == 0.000001


def test_score_orders_lexicographically():
    S = T.Score
    assert S(0, 5, 100.0, 50, 9000.0, 99) < S(1, 0, 0.0, 0, 0.0, 0)            # a DRC error beats everything
    assert S(0, 1, 0.0, 99, 9999.0, 99) < S(0, 2, 0.0, 0, 0.0, 0)              # then opens
    assert S(0, 1, 1.0, 9, 9.0, 9) < S(0, 1, 2.0, 0, 0.0, 0)                   # then necked length
    assert S(0, 1, 1.0, 3, 500.0, 9) < S(0, 1, 1.0, 4, 10.0, 0)                # then vias
    assert S(0, 1, 1.0, 3, 10.0, 9) < S(0, 1, 1.0, 3, 10.5, 0)                 # then length
    assert S(0, 1, 1.0, 3, 10.0, 2) < S(0, 1, 1.0, 3, 10.0, 3)                 # then bends
    assert S(0, 1, 1.0, 3, 10.0, 2) == S(0, 1, 1.0, 3, 10.0, 2)
    assert not S(0, 1, 1.0, 3, 10.0, 2) < S(0, 1, 1.0, 3, 10.0, 2)
    assert min([S(1, 0), S(0, 3), S(0, 2, 5.0)]) == S(0, 2, 5.0)
    assert S().key() == (0, 0, 0.0, 0, 0.0, 0)
    assert [f.name for f in dataclasses.fields(S)] == ["new_errors", "opens", "necked_mm", "vias", "length_mm", "bends"]


def test_budget_charge_sub_and_no_clock():
    b = T.Budget(1000)
    assert b.left() == 1000 and not b.spent_out()
    b.charge(100)
    b.charge(0)
    b.charge(-5)                       # never refunds
    assert b.left() == 900 and b.used == 100
    c = b.sub(0.45)                    # 45 % of the total, charged to the parent too
    assert c.total == 450 and c.left() == 450
    c.charge(50)
    assert c.left() == 400 and b.left() == 850
    g = c.sub(0.5)                     # grandchild: charges go all the way up
    assert g.total == 225
    g.charge(25)
    assert (g.left(), c.left(), b.left()) == (200, 375, 825)
    # unused share rolls forward: the next stage's share is taken from what is really left
    c.charge(10)
    d = b.sub(0.9)
    assert d.total == b.left() == 815   # 900 asked, 815 left
    d.charge(815)
    assert b.spent_out() and b.left() == 0 and d.spent_out()
    # a floor is a guaranteed minimum, even when the parent is spent out (the parent goes over, never negative left)
    e = b.sub(0.1, floor=200)
    assert e.total == 200 and not e.spent_out()
    e.charge(200)
    assert e.spent_out() and b.left() == 0 and b.used == 1200
    assert T.Budget(-5).total == 0 and T.Budget(-5).spent_out()
    # same charges, same state: nothing in the module can read a clock or a random source
    src = Path(T.__file__).read_text(encoding="utf-8")
    for banned in ("import time", "from time", "import random", "datetime", "perf_counter", "monotonic"):
        assert banned not in src, banned
    assert abs(sum(T.STAGE_SHARE.values()) - 1.0) < 1e-9
    assert T.WORK_PER_SECOND == 200_000


def test_budget_matches_v1_scale():
    from copper_kicad.pcb.guided import router
    assert T.WORK_PER_SECOND == router.WORK_PER_SECOND and T.CHARGE_SEARCH == router.SEARCH_WORK


def test_piece_is_frozen_and_typed():
    p = T.Piece(id=1, net="A", conn=3, kind="seg", layer=0, x1=1.0, y1=2.0, x2=4.0, y2=6.0, w=0.25)
    assert p.length == 5.0 and p.target_w == 0.25 and p.stage == "detail" and not p.protected and p.drill == 0.0
    try:
        p.w = 0.3
        raise AssertionError("Piece must be frozen")
    except dataclasses.FrozenInstanceError:
        pass
    n = dataclasses.replace(p, w=0.15, target_w=0.25)
    assert n.w == 0.15 and n.target_w == 0.25 and n.id == 1
    v = T.Piece(id=2, net="A", conn=-1, kind="via", layer=-1, x1=1.0, y1=2.0, x2=1.0, y2=2.0, w=0.6, drill=0.3,
                stage="power", protected=True)
    assert v.length == 0.0 and v.target_w == 0.6
    assert hash(p) != hash(v) and p == dataclasses.replace(p) and len({p, dataclasses.replace(p)}) == 1
    try:
        T.Piece(id=3, net="A", conn=0, kind="arc", layer=0, x1=0, y1=0, x2=1, y2=1, w=0.2)
        raise AssertionError("unknown kind must be refused")
    except ValueError:
        pass
    names = [f.name for f in dataclasses.fields(T.Piece)]
    assert names == ["id", "net", "conn", "kind", "layer", "x1", "y1", "x2", "y2", "w", "drill", "stage", "protected",
                     "target_w"], names


def test_shared_records_have_the_design_fields():
    def fields(cls):
        return [f.name for f in dataclasses.fields(cls)]
    assert fields(T.Terminal) == ["x", "y", "layers", "pad", "cluster", "escape"]
    assert fields(T.ConnV2) == ["idx", "net", "a", "b", "est", "role", "width", "neck", "clearance", "via", "rank",
                                "tree_target"]
    assert fields(T.Corridor) == ["conn", "tiles", "layers", "layer_seq", "lateral", "overflow"]
    assert fields(T.Blocker) == ["rule", "net", "tag", "static", "piece", "dist", "need", "at", "layer"]
    assert fields(T.Cut) == ["a", "b", "layer", "capacity_mm", "demand_mm", "nets"]
    assert fields(T.StageReport) == ["name", "routed", "failed", "score", "work", "notes", "data"]
    assert fields(T.Ctx) == ["board", "cfg", "x", "oracle", "state", "conns", "corridors", "tiles", "history", "reports",
                             "progress"]
    a = T.Terminal(1.0, 2.0, (0,), pad="U1.1", cluster=4)
    c = T.ConnV2(idx=0, net="A", a=a, b=T.Terminal(5.0, 2.0, (0, 1)))
    assert c.role == "signal" and c.tree_target and c.rank == () and not a.escape
    # mutable defaults are per instance
    r1, r2 = T.StageReport("detail"), T.StageReport("repair")
    r1.notes.append("x")
    assert r2.notes == [] and r1.score == T.Score() and r1.score is not r2.score
    x1, x2 = T.Ctx(board=None, cfg=None), T.Ctx(board=None, cfg=None)
    x1.corridors[1] = T.Corridor(conn=1, tiles=frozenset({(0, 0)}))
    assert x2.corridors == {} and x2.conns == [] and x2.tiles is None
    assert set(T.ROLE_ORDER) == set(T.ROLES) and T.ROLE_ORDER["critical"] < T.ROLE_ORDER["signal"] < T.ROLE_ORDER["plane"]
    assert 0 < T.TOL_MM < 0.0005 and T.GUARD_MM == 0.002 and T.PROTECT_FACTOR == 8


def test_every_v2_module_imports():
    """W0's stubs: the whole package is importable, so the work packages can land in any order."""
    import importlib
    from copper_kicad.pcb import v2
    for name in v2.MODULES:
        importlib.import_module(f"copper_kicad.pcb.v2.{name}")
    from copper_kicad.pcb.v2 import engine
    st = engine.available_stages()
    assert set(st) == set(engine.STAGES) and all(isinstance(v, bool) for v in st.values())


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
    sys.exit(1 if fails else 0)
