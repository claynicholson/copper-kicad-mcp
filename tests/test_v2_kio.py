"""Router v2 KiCad I/O (design §9 kio.py): commit to a scratch board, uuids in input order, new-error diff,
marker -> piece blame by uuid and by proximity.

Pure tests (fake DRC data), plus tests on a scratch copy of the corpus board ``ecc83`` that need KiCad 10
(worker + kicad-cli) and the benchmark corpus; those are skipped when either is missing.

Run:  python tests/test_v2_kio.py [test_name ...]
Scratch files go to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import hashlib
import json
import re
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
from copper_kicad import project as projmod  # noqa: E402
from copper_kicad.pcb.guided import model, plan as planmod, session  # noqa: E402
from copper_kicad.pcb.v2 import kio, legacy, oracle as O, pieces as PS  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Ctx, Terminal  # noqa: E402


# ── pure ──────────────────────────────────────────────────────────────────
def viol(typ, desc, items, severity="error"):
    return {"type": typ, "description": desc, "severity": severity,
            "items": [{"description": d, "uuid": u, "pos": {"x": x, "y": y}} for d, u, x, y in items]}


def small_state():
    ex = {"layers": ["F.Cu", "B.Cu"],
          "rules": {"clearance": 0.15, "track_width": 0.15, "via": 0.6, "drill": 0.3, "edge_clearance": 0.3,
                    "hole_clearance": 0.25, "hole_to_hole": 0.25, "annular": 0.1, "default": {}},
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3}
                   for i, n in enumerate(("A", "B"))},
          "outline": {"closed": True, "polys": [{"o": [[0, 0], [30, 0], [30, 20], [0, 20]]}]},
          "pads": [{"ref": r, "num": "1", "net": n, "x": x, "y": y, "kind": "smd", "layers": [0],
                    "shapes": [[[0], "box", [x, y, 1, 0, 0.4, 0.4, 0.0]]], "c": i + 1}
                   for i, (r, n, x, y) in enumerate((("A1", "A", 5, 5), ("A2", "A", 25, 5), ("B1", "B", 5, 15),
                                                     ("B2", "B", 25, 15)))],
          "footprints": [], "graphics": [], "tracks": [], "vias": [], "zones": []}
    b = model.Board(ex)
    o = O.Oracle(b)

    def t(name):
        p = next(p for p in b.pads if p.name == name)
        return Terminal(x=p.x, y=p.y, layers=(0,), pad=name, cluster=p.cluster)
    st = PS.RouteState(o, [ConnV2(idx=0, net="A", a=t("A1.1"), b=t("A2.1")), ConnV2(idx=1, net="B", a=t("B1.1"), b=t("B2.1"))])
    return st


def test_new_markers_is_a_multiset_difference_that_keeps_uuids():
    base = [viol("clearance", "Clearance violation (0.2 > 0.1)", [("Pad 1 of U1", "u-pad1", 1.0, 1.0), ("Pad 2 of U1", "u-pad2", 1.5, 1.0)]),
            viol("silk_overlap", "Silk overlap", [("Text", "u-t", 3.0, 3.0)], "warning")]
    raw = [base[0],
           viol("clearance", "Clearance violation (0.2 > 0.1)", [("Pad 1 of U1", "u-pad1", 1.0, 1.0), ("Pad 2 of U1", "u-pad2", 1.5, 1.0)]),
           viol("shorting_items", "Items shorting two nets", [("Zone [GND] on F.Cu", "u-z", 0.0, 0.0), ("Track [A] on F.Cu", "u-trk", 9.0, 4.0)]),
           viol("clearance", "Clearance violation (0.2 > 0.05)", [("Track [A] on F.Cu", "u-trk", 7.0, 2.0), ("Via [B]", "u-via", 7.1, 2.0)]),
           viol("starved_thermal", "Thermal relief starved", [("Zone [GND]", "u-z", 0.0, 0.0), ("Pad 1 [GND] of C3", "u-c3", 4.0, 4.0)]),
           viol("silk_overlap", "Silk overlap", [("Text", "u-t2", 3.0, 3.0)], "warning")]
    new = kio.new_markers(raw, base)
    # one of the two identical clearance errors was there before; warnings never count
    assert [m["type"] for m in new] == ["clearance", "clearance", "shorting_items", "starved_thermal"]
    # canonical order: (type, x, y, message)
    assert [m.get("at") for m in new] == [[1.0, 1.0], [7.0, 2.0], [9.0, 4.0], [4.0, 4.0]]
    m = new[1]
    assert m == {"type": "clearance", "message": "Clearance violation (0.2 > 0.05)", "items": ["Track [A] on F.Cu", "Via [B]"],
                 "uuids": ["u-trk", "u-via"], "pos": [[7.0, 2.0], [7.1, 2.0]], "copper": True, "at": [7.0, 2.0]}
    # "where to look" is the track, not the zone corner; a thermal error is not a copper-rule type
    assert new[2]["at"] == [9.0, 4.0] and new[2]["uuids"] == ["u-z", "u-trk"] and new[3]["copper"] is False
    # the same keys session._new_errors gives (so drc.new_top keeps its shape), plus uuids / pos / copper
    old = session._new_errors(raw, base)
    assert len(old) == len(new)
    for k in ("type", "message", "items", "at"):
        assert sorted(json.dumps(e.get(k)) for e in old) == sorted(json.dumps(e.get(k)) for e in new)
    assert kio.new_markers(base, base) == [] and kio.new_markers([], base) == [] and kio.new_markers(raw, raw) == []
    assert len(kio.new_markers(raw, [])) == 5


def test_marker_pieces_by_uuid_then_by_proximity():
    st = small_state()
    a = st.add(net="A", conn=0, kind="seg", layer=0, x1=5.0, y1=5.0, x2=25.0, y2=5.0, w=0.2)
    v = st.add(net="A", conn=0, kind="via", x1=15.0, y1=5.0, w=0.6, drill=0.3)
    b = st.add(net="B", conn=1, kind="seg", layer=0, x1=5.0, y1=15.0, x2=25.0, y2=15.0, w=0.2)
    tracks, vias, ids = st.export()
    assert ids == [a.id, b.id, v.id]
    uuids = ["u-a", "u-b", "u-v"]                        # commit order: tracks, then vias
    # by uuid: exactly the items KiCad names, whatever is near
    m = {"type": "clearance", "uuids": ["u-pad", "u-b"], "pos": [[15.0, 5.0], [15.0, 5.1]], "at": [15.0, 5.0]}
    assert kio.marker_pieces(m, uuids, ids, st) == [b.id]
    m = {"type": "clearance", "uuids": ["u-v", "u-a"], "pos": [[1.0, 1.0]]}
    assert kio.marker_pieces(m, uuids, ids, st) == [a.id, v.id]
    # a raw kicad-cli violation works too
    raw = viol("clearance", "c", [("Track", "u-b", 9.0, 15.0), ("Pad", "u-x", 9.0, 15.3)])
    assert kio.marker_pieces(raw, uuids, ids, st) == [b.id]
    # no item of ours: everything whose copper comes within 0.3 mm of a marker position
    m = {"type": "starved_thermal", "uuids": ["u-zone", "u-pad"], "pos": [[0.0, 0.0], [15.0, 5.35]], "at": [15.0, 5.35]}
    assert kio.marker_pieces(m, uuids, ids, st) == [a.id, v.id]      # track edge 0.25 away, via edge 0.05
    m = {"type": "starved_thermal", "uuids": [], "pos": [[10.0, 5.39]]}
    assert kio.marker_pieces(m, uuids, ids, st) == [a.id]            # 0.29 mm from the track's edge
    m["pos"] = [[10.0, 5.45]]
    assert kio.marker_pieces(m, uuids, ids, st) == []                # 0.35 mm
    assert kio.marker_pieces(m, uuids, ids, st, radius=0.5) == [a.id]
    assert kio.marker_pieces({"type": "x"}, uuids, ids, st) == []
    # a blamed uuid whose piece has been ripped since falls back to proximity
    st.remove(b.id)
    m = {"type": "clearance", "uuids": ["u-b"], "pos": [[15.0, 5.0]]}
    assert kio.marker_pieces(m, uuids, ids, st) == [a.id, v.id]


# ── with KiCad ────────────────────────────────────────────────────────────
def have_kicad() -> bool:
    return common.KICAD_PY.exists() and common.KICAD_CLI.exists() and (common.CORPUS / "ecc83").exists()


def project(name="ecc83"):
    work = common.WORK / "test_v2_kio" / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for f in (common.CORPUS / name).iterdir():
        if f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            shutil.copy2(f, work / f.name)
    return projmod.Project(work, name)


def sha(path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


_SEG = re.compile(r'\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)(.*?)\(uuid "([^"]+)"\)', re.S)
_VIA = re.compile(r'\(via\s+\(at ([-\d.]+) ([-\d.]+)\)(.*?)\(uuid "([^"]+)"\)', re.S)


def copper_in_file(pcb):
    txt = Path(pcb).read_text(encoding="utf-8")
    segs = {m.group(6): tuple(float(m.group(i)) for i in range(1, 5)) for m in _SEG.finditer(txt)}
    vias = {m.group(4): (float(m.group(1)), float(m.group(2))) for m in _VIA.finditer(txt)}
    return segs, vias


def test_commit_uuids_blame_and_fresh_scratch():
    if not have_kicad():
        print("    (skipped: KiCad or the benchmark corpus not found)")
        return
    pr = project()
    before = sha(pr.pcb)
    # extract is read-only, with or without a rip-up
    ex = kio.extract(pr)
    assert ex["_ripped"] == 0 and ex["unconnected"] == 14 and sha(pr.pcb) == before
    base_raw = kio.baseline_drc(pr)
    assert isinstance(base_raw, list) and sha(pr.pcb) == before
    b = model.Board(ex, model.dru_minimums(pr.dir / f"{pr.name}.kicad_dru"))
    cfg = planmod.resolve(b, planmod.default_plan(b, planmod.classify(b)))
    o = O.Oracle(b, overrides=O.local_overrides_from_file(pr.pcb))
    conns = legacy.plan_connections(b, cfg)
    st = PS.RouteState(o, conns)
    ctx = Ctx(board=b, cfg=cfg, oracle=o, state=st, conns=conns)
    assert legacy.route_all(ctx, Budget(20_000_000)).failed == 0
    # one via so both lists are exercised: on the first pad's net, somewhere the oracle allows
    net0 = conns[0].net
    via = None
    for p in st.pieces(net=net0):
        if p.kind == "seg" and o.via_ok(p.x1, p.y1, 0.8, 0.4, net0) is None:
            via = st.add(net=net0, conn=conns[0].idx, kind="via", x1=p.x1, y1=p.y1, w=0.8, drill=0.4)
            break
    sc = kio.scratch(pr)
    try:
        assert Path(sc.pcb).resolve() != Path(pr.pcb).resolve()
        tracks, vias, ids = st.export()
        res = kio.commit_and_check(pr, sc, tracks, vias, base_raw)
        assert sha(pr.pcb) == before, "the project's own board was written"
        # uuids come back in input order: track i of the request is the segment with uuid i in the file
        assert len(res["uuids"]) == len(ids) == len(tracks) + len(vias)
        assert res["uuids"] == res["track_uuids"] + res["via_uuids"] and len(set(res["uuids"])) == len(ids)
        segs, vs = copper_in_file(sc.pcb)
        assert len(segs) == len(tracks) and len(vs) == len(vias)
        for u, t in zip(res["track_uuids"], tracks):
            assert all(abs(a - b_) < 1e-6 for a, b_ in zip(segs[u], t[2:6])), (u, segs[u], t)
        for u, v in zip(res["via_uuids"], vias):
            assert all(abs(a - b_) < 1e-6 for a, b_ in zip(vs[u], v[1:3])), (u, vs[u], v)
        # KiCad agrees with the router: nothing new, nothing unconnected
        assert res["new"] == [], res["new"]
        assert res["stats"]["unconnected"] == 0 and not any(res["unconnected"].values())
        assert res["ex"]["totals"]["tracks"] == len(tracks) and isinstance(res["raw"], list)
        clean_uuids = list(res["uuids"])

        # a seeded short: a track of the first net straight onto a pad of another net
        other = next(p for p in b.pads if p.net and b.net_by_id[p.net].name != net0 and 0 in p.layers)
        mine = next(p for p in b.nets[net0].pads if 0 in p.layers)
        bad = st.add(net=net0, conn=-1, kind="seg", layer=0, x1=mine.x, y1=mine.y, x2=other.x, y2=other.y, w=0.25)
        assert o.piece_ok(bad) is not None                      # the oracle saw it coming
        tracks2, vias2, ids2 = st.export()
        res2 = kio.commit_and_check(pr, sc, tracks2, vias2, base_raw)
        segs2, _vs2 = copper_in_file(sc.pcb)
        assert len(segs2) == len(tracks2), "fresh=True must reset the scratch board, not add to the last commit"
        assert not set(res2["uuids"]) & set(clean_uuids)
        assert res2["new"], "KiCad must flag the seeded short"
        assert all(set(m) >= {"type", "message", "items", "uuids", "pos", "copper"} for m in res2["new"])
        bad_uuid = res2["uuids"][ids2.index(bad.id)]
        named = [m for m in res2["new"] if bad_uuid in m["uuids"]]
        assert named and all(bad.id in kio.marker_pieces(m, res2["uuids"], ids2, st) for m in named)
        blamed = set()
        for m in res2["new"]:
            if m["copper"]:
                blamed.update(st.piece(i).id for i in kio.marker_pieces(m, res2["uuids"], ids2, st))
        assert bad.id in blamed
        # without uuids the same marker still finds the piece by position
        m0 = dict(named[0], uuids=[])
        assert bad.id in kio.marker_pieces(m0, res2["uuids"], ids2, st)

        # incremental: remove the bad track from the scratch board, add nothing -> clean again
        res3 = kio.commit_and_check(pr, sc, [], [], base_raw, fresh=False, remove=[bad_uuid])
        segs3, _ = copper_in_file(sc.pcb)
        assert len(segs3) == len(tracks) and res3["uuids"] == [] and res3["new"] == [], res3["new"]
        # extract with a rip-up is in memory only
        h = sha(sc.pcb)
        ex2 = kio.extract(sc, "all")
        assert ex2["_ripped"] == len(tracks) + len(vias) and not ex2["tracks"] and sha(sc.pcb) == h
        base2 = kio.baseline_drc(sc, "all")
        assert sha(sc.pcb) == h and len(kio.new_markers(base2, base_raw)) == 0
    finally:
        kio.cleanup(sc)
    assert not Path(sc.dir).exists() and sha(pr.pcb) == before


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
