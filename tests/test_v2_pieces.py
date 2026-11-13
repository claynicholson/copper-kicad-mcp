"""Router v2 RouteState (design §4.2): transactional copper, connection-level rip, connectivity, score, export.

Pure Python on synthetic boards (no KiCad).
Run:  python tests/test_v2_pieces.py [test_name ...] [--perf]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import model  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2 import pieces as PS  # noqa: E402
from copper_kicad.pcb.v2.types import ConnV2, Piece, Score, Terminal  # noqa: E402


# ── synthetic boards ──────────────────────────────────────────────────────
def board(pads, *, size=(40.0, 30.0), layers=("F.Cu", "B.Cu"), tracks=(), vias=(), zones=()):
    """pads: (ref, net, x, y[, 'tht'[, cluster]]) — 0.8 mm square pads on F.Cu (THT: every layer).
    Each pad is its own cluster unless a cluster id is given."""
    nets = sorted({p[1] for p in pads if p[1]} | {t[0] for t in tracks} | {v[0] for v in vias}
                  | {z["net"] for z in zones if z.get("net")})
    ex = {"layers": list(layers),
          "rules": {"clearance": 0.15, "track_width": 0.15, "via": 0.6, "drill": 0.3, "edge_clearance": 0.3,
                    "hole_clearance": 0.25, "hole_to_hole": 0.25, "annular": 0.1,
                    "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.2, "dp_width": 0.2}},
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.2, "dp_width": 0.2} for i, n in enumerate(nets)},
          "outline": {"closed": True, "polys": [{"o": [[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [list(t) for t in tracks],
          "vias": [list(v) for v in vias], "zones": [dict(z) for z in zones],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    for i, p in enumerate(pads):
        ref, net, x, y = p[:4]
        tht = len(p) > 4 and p[4] == "tht"
        lays = list(range(len(layers))) if tht else [0]
        d = {"ref": ref, "num": "1", "net": net, "x": x, "y": y, "kind": "tht" if tht else "smd", "layers": lays,
             "shapes": [[lays, "box", [x, y, 1, 0, 0.4, 0.4, 0.0]]], "c": p[5] if len(p) > 5 else i + 1}
        if tht:
            d["hole"] = [x, y, 1, 0, 0.2, 0.2]
        ex["pads"].append(d)
    return model.Board(ex)


def term(b, name):
    p = next(p for p in b.pads if p.name == name)
    return Terminal(x=p.x, y=p.y, layers=tuple(p.layers), pad=p.name, cluster=p.cluster)


def conn(b, idx, net, a, bname=None, **kw):
    tb = term(b, bname) if bname else Terminal(x=0.0, y=0.0, layers=())
    return ConnV2(idx=idx, net=net, a=term(b, a), b=tb, width=0.2, neck=0.2, clearance=0.2, via=(0.6, 0.3), **kw)


PADS = [("A1", "A", 5.0, 5.0), ("A2", "A", 15.0, 5.0), ("A3", "A", 10.0, 12.0, "tht"),
        ("B1", "B", 5.0, 8.0), ("B2", "B", 15.0, 8.0)]


def fresh(pads=PADS, **kw):
    b = board(pads, **kw)
    o = O.Oracle(b)
    conns = [conn(b, 0, "A", "A1.1", "A2.1"), conn(b, 1, "A", "A3.1", "A2.1"), conn(b, 2, "B", "B1.1", "B2.1")]
    return b, o, PS.RouteState(o, conns)


def seg(st, c, net, lay, x1, y1, x2, y2, w=0.2, **kw):
    return st.add(net=net, conn=c, kind="seg", layer=lay, x1=x1, y1=y1, x2=x2, y2=y2, w=w, **kw)


def probes(o):
    """Answers of the oracle at a few places (version-independent: only legality and the blocker's identity)."""
    out = []
    for lay, x1, y1, x2, y2, net in ((0, 4.0, 6.5, 16.0, 6.5, "B"), (0, 10.0, 2.0, 10.0, 9.0, "B"),
                                     (1, 4.0, 6.5, 16.0, 6.5, "B"), (0, 4.0, 6.5, 16.0, 6.5, "A"),
                                     (0, 8.0, 5.3, 12.0, 5.3, "B")):
        blk = o.seg_ok(lay, x1, y1, x2, y2, 0.2, net)
        out.append(None if blk is None else (blk.rule, blk.net, blk.piece, blk.dist))
    v = o.via_ok(10.0, 5.5, 0.6, 0.3, "B")
    out.append(None if v is None else (v.rule, v.net, v.piece, v.dist))
    return out


# ── tests ─────────────────────────────────────────────────────────────────
def test_add_remove_mirror_the_oracle():
    b, o, st = fresh()
    base = probes(o)
    p1 = seg(st, 0, "A", 0, 5.0, 5.0, 15.0000004, 5.0)
    assert isinstance(p1, Piece) and p1.id == 1 and p1.x2 == 15.0 and p1.stage == "detail" and p1.target_w == 0.2
    p2 = st.add(net="A", conn=1, kind="via", x1=10.0, y1=5.0, w=0.6, drill=0.3)
    assert p2.id == 2 and p2.layer == -1 and (p2.x2, p2.y2) == (10.0, 5.0)
    assert o.has_piece(1) and o.has_piece(2) and len(st) == 2
    # a B track across the A track is a short against piece 1; the A via blocks B on the back layer too
    blk = o.seg_ok(0, 10.5, 2.0, 10.5, 7.0, 0.2, "B")
    assert blk is not None and blk.piece in (1, 2) and not blk.static
    assert o.seg_ok(1, 10.0, 2.0, 10.0, 7.0, 0.2, "B").piece == 2
    assert st.pieces() == [p1, p2] and st.pieces(conn=0) == [p1] and st.pieces(net="A") == [p1, p2]
    assert st.pieces(net="B") == [] and st.piece(2) is p2 and st.piece(99) is None
    st.remove(1)
    st.remove(2)
    assert probes(o) == base and len(st) == 0 and not o.has_piece(1)
    p3 = seg(st, 0, "A", 0, 5.0, 5.0, 15.0, 5.0)
    assert p3.id == 3                                    # ids are never reused
    try:
        st.remove(1)
        raise AssertionError("removing a piece that is gone must raise")
    except KeyError:
        pass
    try:
        st.add(net="A", conn=0, kind="seg", layer=0, x1=0, y1=0, x2=1, y2=1, w=0.2, colour="red")
        raise AssertionError("unknown field must raise")
    except TypeError:
        pass


def test_nested_rollback_restores_oracle_answers():
    b, o, st = fresh()
    seg(st, 0, "A", 0, 5.0, 5.0, 15.0, 5.0)
    st.set_status(0, "routed")
    base = probes(o)
    snap0 = st.snapshot()
    v0 = o.version
    t1 = st.begin()
    seg(st, 2, "B", 0, 5.0, 8.0, 15.0, 8.0)
    st.set_status(2, "routed")
    st.new_errors = 3
    mid = probes(o)
    mid_snap = st.snapshot()
    t2 = st.begin()
    st.remove(1)
    seg(st, 1, "A", 1, 10.0, 12.0, 10.0, 6.0)
    st.add(net="A", conn=1, kind="via", x1=10.0, y1=6.0, w=0.6, drill=0.3)
    st.set_status(0, "failed", {"reason": "x"})
    st.new_errors = 7
    assert probes(o) != mid and st.depth == 2
    st.rollback(t2)
    assert probes(o) == mid and st.snapshot() == mid_snap and st.new_errors == 3 and st.depth == 1
    assert st.status(0) == "routed" and st.fail(0) is None
    st.rollback(t1)
    assert probes(o) == base and st.snapshot() == snap0 and st.new_errors == 0 and st.status(2) == "todo"
    assert o.version > v0                                # the version moved on; the answers did not
    assert [p.id for p in st.pieces()] == [1]
    # an inner commit stays undoable by the outer transaction; an outer commit closes inner ones
    t1 = st.begin()
    t2 = st.begin()
    seg(st, 2, "B", 0, 5.0, 8.0, 15.0, 8.0)
    st.commit(t2)
    st.rollback(t1)
    assert st.snapshot() == snap0 and probes(o) == base
    t1 = st.begin()
    st.begin()
    p = seg(st, 2, "B", 0, 5.0, 8.0, 15.0, 8.0)
    st.commit(t1)
    assert st.depth == 0 and st.piece(p.id) is p and not st._log
    for bad in (1, 0, "x"):
        try:
            st.rollback(bad)
            raise AssertionError("a closed token must raise")
        except ValueError:
            pass
    # outside a transaction nothing is logged
    seg(st, 2, "B", 1, 5.0, 9.0, 15.0, 9.0)
    assert not st._log


def test_snapshot_restore():
    b, o, st = fresh()
    seg(st, 0, "A", 0, 5.0, 5.0, 15.0, 5.0)
    seg(st, 2, "B", 0, 5.0, 8.0, 15.0, 8.0)
    st.set_status(0, "routed")
    st.set_status(2, "failed", {"reason": "blocked"})
    st.new_errors = 2
    snap = st.snapshot()
    sc = st.score()
    ans = probes(o)
    try:
        snap.pieces = ()
        raise AssertionError("snapshots are immutable")
    except AttributeError:
        pass
    st.remove(2)
    seg(st, 2, "B", 1, 5.0, 8.0, 15.0, 8.0)
    seg(st, 1, "A", 1, 10.0, 12.0, 10.0, 6.0)
    st.set_status(2, "routed")
    st.set_status(1, "routed")
    st.new_errors = 0
    other = st.snapshot()
    assert other != snap and st.score() != sc
    st.restore(snap)
    assert st.snapshot() == snap and st.score() == sc and probes(o) == ans
    assert [p.id for p in st.pieces()] == [1, 2] and st.fail(2) == {"reason": "blocked"} and st.status(1) == "todo"
    # restore is an edit like any other: undoable
    t = st.begin()
    st.restore(other)
    assert st.snapshot() == other
    st.rollback(t)
    assert st.snapshot() == snap and probes(o) == ans
    # a piece added after a restore gets a fresh id
    assert seg(st, 1, "A", 1, 10.0, 12.0, 10.0, 6.0).id == 5


def test_connected_tees_vias_and_static_copper():
    b, o, st = fresh()
    assert not st.connected(0) and not st.connected(1)
    # A1 -> A2 on F.Cu in two segments
    seg(st, 0, "A", 0, 5.0, 5.0, 10.0, 5.0)
    assert not st.connected(0)
    seg(st, 0, "A", 0, 10.0, 5.0, 15.0, 5.0)
    assert st.connected(0) and not st.connected(1) and not st.connected(2)
    # A3 (THT) on B.Cu up to under the track, via as a tee in the MIDDLE of the first segment
    s3 = seg(st, 1, "A", 1, 10.0, 12.0, 8.0, 10.0)
    seg(st, 1, "A", 1, 8.0, 10.0, 8.0, 5.0)
    assert not st.connected(1)                           # other layer: no via yet
    v = st.add(net="A", conn=1, kind="via", x1=8.0, y1=5.0, w=0.6, drill=0.3)
    assert st.connected(1) and st.connected(1, loose=True)
    st.remove(v.id)
    assert not st.connected(1)
    # a tee: a segment end on the centre line of another segment of the net (same layer)
    t = seg(st, 1, "A", 0, 10.0, 12.0, 12.0, 5.0)          # A3 is THT: F.Cu works too
    assert st.connected(1)
    st.remove(t.id)
    # 1 nm off the centre line still is a tee; 50 um off is a join only by KiCad's rule (inside the copper)
    t = seg(st, 1, "A", 0, 10.0, 12.0, 12.0, 5.000001)
    assert st.connected(1)
    st.remove(t.id)
    t = seg(st, 1, "A", 0, 10.0, 12.0, 12.0, 5.05)
    assert not st.connected(1) and st.connected(1, loose=True)
    st.remove(t.id)
    t = seg(st, 1, "A", 0, 10.0, 12.0, 12.0, 5.2)          # outside the 0.2 mm track
    assert not st.connected(1, loose=True)
    st.remove(t.id)
    # a different net never joins
    seg(st, 2, "B", 0, 5.0, 8.0, 15.0, 8.0)
    assert st.connected(2) and not st.connected(1)
    # ripping connection 0 disconnects it, and connection 1 with its tee would dangle
    t = seg(st, 1, "A", 0, 10.0, 12.0, 12.0, 5.0)
    for i in (0, 1):
        st.set_status(i, "routed")
    assert st.dangling("A") == []
    ripped = st.rip_conn(0)
    assert [p.id for p in ripped] == [1, 2] and st.status(0) == "todo"
    assert st.dangling("A") == [1] and not st.connected(1) and not st.connected(0)
    assert s3.id in [p.id for p in st.pieces(conn=1)]


def test_connected_static_tracks_pours_and_open_terminals():
    # A1 and A2 have kept tracks (cluster copper); A3's cluster owns a pour island on B.Cu
    tracks = [["A", 0, 5.0, 5.0, 8.0, 5.0, 0.3, 1, False]]
    vias = [["A", 15.0, 6.5, 0.6, 0.3, 0, 1, 2, False]]
    zone = {"net": "A", "layers": [1], "name": "P", "filled": True, "polys": [{"o": [[20, 2], [38, 2], [38, 28], [20, 28]]}],
            "fills": {"1": [{"o": [[20, 2], [38, 2], [38, 28], [20, 28]]}]}}
    pads = [("A1", "A", 5.0, 5.0), ("A2", "A", 15.0, 5.0), ("A3", "A", 30.0, 12.0, "tht"), ("B1", "B", 5.0, 8.0)]
    b = board(pads, tracks=tracks, vias=vias, zones=[zone])
    o = O.Oracle(b)
    assert b.clusters["A"][3]["islands"] and b.clusters["A"][2]["vias"] and b.clusters["A"][1]["tracks"]
    conns = [conn(b, 0, "A", "A1.1", "A2.1"), conn(b, 1, "A", "A2.1"), conn(b, 2, "A", "A1.1")]
    assert PS.is_open(conns[1].b) and not PS.is_open(conns[0].b)
    st = PS.RouteState(o, conns)
    # from the END of A1's kept track (inside its copper, off the pad) to A2's kept via
    seg(st, 0, "A", 0, 8.1, 5.0, 12.0, 5.0)
    assert not st.connected(0) and not st.connected(2)
    seg(st, 0, "A", 0, 12.0, 5.0, 15.0, 6.6)              # ends 0.1 mm inside the 0.6 mm via
    assert st.connected(0) and st.connected(2) and st.connected(1)     # open: A2 reached another cluster
    st.rip_conn(0)
    assert not st.connected(1)
    # a via dropped anywhere into the pour island joins A3's cluster
    seg(st, 1, "A", 0, 15.0, 5.0, 25.0, 5.0)
    assert not st.connected(1)
    v = st.add(net="A", conn=1, kind="via", x1=25.0, y1=5.0, w=0.6, drill=0.3)
    assert st.connected(1) and not st.connected(0)
    assert st.net_groups("A") == [([2, 3], [3, v.id])]
    st.remove(v.id)
    assert st.net_groups("A") == [([2], [3])]
    # a segment that ends on B.Cu inside the island joins it too; on F.Cu it does not
    seg(st, 1, "A", 0, 25.0, 5.0, 25.0, 6.0)
    assert not st.connected(1)


def test_score_is_exact_and_order_independent():
    b, o, st = fresh()
    assert st.score() == Score(opens=3)
    seg(st, 0, "A", 0, 5.0, 5.0, 10.0, 5.0)
    seg(st, 0, "A", 0, 10.0, 5.0, 12.0, 7.0)              # 45 degree bend
    seg(st, 0, "A", 0, 12.0, 7.0, 14.0, 7.0, w=0.15, target_w=0.2)   # bend + necked
    seg(st, 0, "A", 0, 14.0, 7.0, 15.0, 7.0)              # collinear: no bend
    st.add(net="A", conn=0, kind="via", x1=15.0, y1=7.0, w=0.6, drill=0.3)
    st.set_status(0, "routed")
    sc = st.score()
    assert sc.opens == 2 and sc.vias == 1 and sc.bends == 2 and sc.new_errors == 0
    assert abs(sc.length_mm - (5.0 + 2.828427 + 2.0 + 1.0)) < 1e-6 and sc.necked_mm == 2.0
    st.new_errors = 1
    assert st.score().new_errors == 1 and st.score() > sc
    st.new_errors = 0
    # the same copper reached by a different add / remove history scores identically (integer nanometres)
    b2, o2, st2 = fresh()
    junk = [seg(st2, 1, "A", 1, 1.0 + 0.1 * i, 20.0, 3.3333333 + 0.1 * i, 22.7777777) for i in range(50)]
    seg(st2, 0, "A", 0, 14.0, 7.0, 15.0, 7.0)
    st2.add(net="A", conn=0, kind="via", x1=15.0, y1=7.0, w=0.6, drill=0.3)
    seg(st2, 0, "A", 0, 12.0, 7.0, 14.0, 7.0, w=0.15, target_w=0.2)
    for p in junk:
        st2.remove(p.id)
    seg(st2, 0, "A", 0, 10.0, 5.0, 12.0, 7.0)
    seg(st2, 0, "A", 0, 5.0, 5.0, 10.0, 5.0)
    st2.set_status(0, "routed")
    assert st2.score() == sc and st2.score().key() == sc.key()
    # failed and todo both count as open
    st.set_status(1, "failed", {"reason": "r"})
    assert st.score().opens == 2
    st.set_status(1, "routed")
    assert st.score().opens == 1
    try:
        st.set_status(1, "done")
        raise AssertionError("unknown status must raise")
    except ValueError:
        pass


def test_rip_conn_is_per_connection_and_keeps_protected_pieces():
    b, o, st = fresh()
    stub = seg(st, 0, "A", 0, 5.0, 5.0, 6.0, 5.0, stage="fanout", protected=True)
    a = seg(st, 0, "A", 0, 6.0, 5.0, 15.0, 5.0)
    other = seg(st, 1, "A", 0, 10.0, 12.0, 10.0, 5.0)
    st.set_status(0, "routed")
    st.set_status(1, "routed")
    assert st.rip_conn(0) == [a]
    assert st.pieces(conn=0) == [stub] and st.pieces(conn=1) == [other]      # the net's other connection stays
    assert st.status(0) == "todo" and st.status(1) == "routed"
    assert st.rip_conn(0, protected=True) == [stub] and st.pieces(conn=0) == []
    assert st.rip_conn(7) == []


def test_export_order_is_stable():
    b, o, st = fresh()
    p1 = seg(st, 0, "A", 0, 5.0, 5.0, 15.0, 5.0)
    v = st.add(net="A", conn=1, kind="via", x1=10.0, y1=5.0, w=0.6, drill=0.3)
    p3 = seg(st, 1, "A", 1, 10.0, 12.0, 10.0, 5.0, w=0.25)
    z = seg(st, 1, "A", 1, 10.0, 5.0, 10.0, 5.0)          # zero length: never written
    p5 = seg(st, 2, "B", 0, 5.0, 8.0, 15.0, 8.0)
    want = ([["A", "F.Cu", 5.0, 5.0, 15.0, 5.0, 0.2], ["A", "B.Cu", 10.0, 12.0, 10.0, 5.0, 0.25],
             ["B", "F.Cu", 5.0, 8.0, 15.0, 8.0, 0.2]], [["A", 10.0, 5.0, 0.6, 0.3]], [p1.id, p3.id, p5.id, v.id])
    assert st.export() == want
    snap = st.snapshot()
    st.remove(p3.id)
    st.remove(p1.id)
    st.remove(z.id)
    st.restore(snap)                                       # pieces come back in another order internally
    assert st.export() == want
    assert st.export(only=[v.id, p5.id]) == ([want[0][2]], want[1], [p5.id, v.id])


def test_same_twice():
    def run():
        b, o, st = fresh()
        t = st.begin()
        seg(st, 0, "A", 0, 5.0, 5.0, 10.0, 5.0)
        seg(st, 0, "A", 0, 10.0, 5.0, 15.0, 5.0)
        t2 = st.begin()
        seg(st, 1, "A", 0, 10.0, 12.0, 10.0, 5.0)
        st.rollback(t2)
        seg(st, 1, "A", 0, 10.0, 12.0, 12.0, 5.0)
        st.commit(t)
        st.set_status(0, "routed")
        return st.export(), st.snapshot().pieces, st.score().key(), st.net_groups("A"), st.connected(1)
    assert run() == run()


def test_throughput():
    """Not a gate: prints the measured cost of the hot operations (RouteState + oracle dynamic layer)."""
    pads = [(f"P{i}", "A" if i % 2 else "B", 3.0 + (i % 30) * 3.0, 3.0 + (i // 30) * 3.0) for i in range(600)]
    b = board(pads, size=(100.0, 70.0))
    o = O.Oracle(b)
    conns = [ConnV2(idx=i, net="A", a=term(b, f"P{2 * i + 1}.1"), b=term(b, f"P{2 * i + 3}.1")) for i in range(250)]
    st = PS.RouteState(o, conns)
    N = 6000
    t0 = time.perf_counter()
    ids = []
    for i in range(N):
        x, y = 2.0 + (i % 95) * 1.0, 2.0 + ((i * 7) % 60) * 1.0 + 0.5
        ids.append(st.add(net="A" if i % 3 else "B", conn=i % 250, kind="seg", layer=i % 2, x1=x, y1=y, x2=x + 0.9,
                          y2=y + (0.9 if i % 2 else 0.0), w=0.2).id)
    t_add = (time.perf_counter() - t0) / N
    t0 = time.perf_counter()
    for k in range(2000):
        st.connected(k % 250)
    t_conn_cached = (time.perf_counter() - t0) / 2000
    t0 = time.perf_counter()
    for k in range(200):
        st.remove(ids[k])
        st.connected(k % 250)                            # forces a component rebuild of the net (4000 pieces)
    t_conn_rebuild = (time.perf_counter() - t0) / 200
    t0 = time.perf_counter()
    for _ in range(20):
        tok = st.begin()
        for k in range(200, 700):
            st.remove(ids[k])
        st.rollback(tok)
    t_rb = (time.perf_counter() - t0) / (20 * 1000)
    t0 = time.perf_counter()
    st.score()                                           # recomputes the bends of every edited connection
    t_score_first = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(2000):
        st.score()
    t_score = (time.perf_counter() - t0) / 2000
    t0 = time.perf_counter()
    snap = st.snapshot()
    t_snap = time.perf_counter() - t0
    t0 = time.perf_counter()
    st.restore(snap)
    t_restore_noop = time.perf_counter() - t0
    t0 = time.perf_counter()
    ex = st.export()
    t_export = time.perf_counter() - t0
    assert len(ex[0]) == len(st) and st.snapshot() == snap
    print(f"    add {t_add * 1e6:.1f} us/piece | remove+re-add in a rolled back transaction {t_rb * 1e6:.1f} us/op | "
          f"connected: cached {t_conn_cached * 1e6:.1f} us, after an edit of a {len(st.pieces(net='A'))}-piece net "
          f"{t_conn_rebuild * 1e3:.2f} ms | score {t_score * 1e6:.1f} us (first after edits of 250 connections "
          f"{t_score_first * 1e3:.2f} ms) | snapshot of {len(st)} pieces "
          f"{t_snap * 1e3:.2f} ms, no-op restore {t_restore_noop * 1e3:.2f} ms, export {t_export * 1e3:.2f} ms")


if __name__ == "__main__":
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t:.1f}s)")
            except Exception:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"{'FAILED' if fails else 'OK'}: {fails} failure(s)")
    sys.exit(1 if fails else 0)
