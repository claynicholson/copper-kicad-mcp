"""Regression tests from the skeptical review of the board features (outline import + elegance).

Each test is a defect that was found by using the tools on boards the feature was not built on
(KiCad's demo projects, hand-made broken CAD files):

  * a D-sub called "..._MountingHolesOffset9.12mm" was taken for a mounting hole and never placed;
  * mounting holes drawn in the schematic stayed where pcb_sync dropped them (outside the board),
    four more were added on top, and the score did not count them as outside;
  * a clean KiCad demo board was capped at 49 by a bounding-box "courtyard overlap" DRC never saw;
  * a self-crossing outline was accepted by the analysis and only stopped by the worker's self check;
  * stitching vias ignored the board's own minimum annular ring (402 DRC errors on one pour);
  * sixteen identical pots scattered at random scored 100.

Run:  python tests/test_board_review.py [test_name ...]      (KiCad's python; needs KiCad 9/10)
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad import project as projmod  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
import copper_kicad.tools.pcb_tools  # noqa: E402,F401 — registers the tools
import copper_kicad.tools.verify_tools  # noqa: E402,F401
from copper_kicad.cad import outline as cadout  # noqa: E402
from copper_kicad.pcb import board, elegant, holes, place, setup_ext  # noqa: E402
from copper_kicad.review import elegance  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="copper-test-boardreview-"))
DSUB = "Connector_Dsub:DSUB-9_Socket_Horizontal_P2.77x2.84mm_EdgePinOffset7.70mm_Housed_MountingHolesOffset9.12mm"
HOLE = "MountingHole:MountingHole_4.3mm_M4"


def call(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    if r["isError"]:
        raise AssertionError(f"{name} failed: {r['content'][0]['text'][:900]}")
    return r.get("structuredContent") or {}, r


def _fp(ref, x, y, w=2.0, h=1.0, rot=0, pads=None, footprint="Resistor_SMD:R_0603", **kw):
    pl = pads if pads is not None else [{"num": "1", "net": f"N{ref}a", "lx": -w / 4, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"},
                                        {"num": "2", "net": f"N{ref}b", "lx": w / 4, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"}]
    d = {"ref": ref, "value": "x", "footprint": footprint, "x": x, "y": y, "rot": rot, "side": "top",
         "lbbox": [-w / 2, -h / 2, w / 2, h / 2], "pad_list": pl}
    d["bbox"] = [x - w / 2, y - h / 2, x + w / 2, y + h / 2]
    d.update(kw)
    return d


def _hole(ref, x, y, **kw):
    return _fp(ref, x, y, w=9.2, h=9.2, footprint=HOLE, pads=[{"num": "1", "net": "", "lx": 0, "ly": 0, "w": 4.3, "h": 4.3, "type": "npth"}], **kw)


def _outline(w=60.0, h=40.0, x0=100.0, y0=100.0):
    return {"bbox": [x0, y0, x0 + w, y0 + h], "width": w, "height": h, "area": w * h, "closed": True,
            "polygon": [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]]}


def _pot(ref, x, y, rot=0):
    pads = [{"num": str(i + 1), "net": f"{ref}n{i}", "lx": -2.5 + 2.5 * i, "ly": 3.0, "w": 1.5, "h": 1.5, "type": "tht"} for i in range(3)]
    return _fp(ref, x, y, w=10.0, h=10.0, rot=rot, pads=pads, footprint="Potentiometer_THT:Pot_9mm")


# ── mounting holes ────────────────────────────────────────────────────────
def test_a_dsub_with_mounting_holes_in_its_name_is_a_connector():
    assert holes.is_hole(HOLE) and holes.is_hole("MountingHole_3.2mm_M3") and holes.is_hole("mylib:Mounting_Hole_M3")
    assert not holes.is_hole(DSUB) and not holes.is_hole("Connector_PinHeader_2.54mm:PinHeader_1x04") and not holes.is_hole("")
    pads = [{"num": str(i + 1), "net": f"S{i}", "lx": -5.5 + 1.4 * i, "ly": 0.0, "w": 1.6, "h": 1.6, "type": "tht"} for i in range(9)]
    j1 = place.Part(_fp("J1", 150, 150, w=31, h=20, pads=pads, footprint=DSUB))
    h1 = place.Part(_hole("P101", 120, 120))
    place.classify([j1, h1])
    assert j1.role == "connector" and not j1.fixed, (j1.role, j1.fixed)
    assert h1.role == "fixed" and h1.fixed
    assert not elegance._is_hole(j1) and elegance._is_hole(h1)
    assert not elegant._is_hole(j1.d) and elegant._is_hole(h1.d)


def test_schematic_holes_get_corner_seats_and_others_stay():
    ol = _outline(80, 60)
    fps = [_hole(f"P10{i}", 130 + 11 * i, 190) for i in range(1, 7)]          # six holes dropped below the board
    seats = holes.seats(6, ol["bbox"], 5.5, ol["polygon"], None, 4.6)
    assert len(seats) == 6 and seats[:4] == [(105.5, 105.5), (174.5, 105.5), (174.5, 154.5), (105.5, 154.5)], seats
    assert seats[4:] == [(140.0, 105.5), (140.0, 154.5)], seats            # then the middles of the long edges
    snap = {"outline": ol, "footprints": list(fps)}
    moves = holes.seat(snap)
    assert sorted(m["ref"] for m in moves) == [f"P10{i}" for i in range(1, 7)]
    got = sorted((m["x"], m["y"]) for m in moves)
    assert got == sorted(seats), got
    assert all(f["y"] == 190 for f in fps), "the caller's footprint dicts must not be mutated"
    # symmetric about both centre lines (what the score's holes_symmetric measures)
    M = elegance.placement_metrics([place.Part(d) for d in snap["footprints"]], ol)
    assert M["holes"] == 6 and M["holes_symmetric"] == 1.0 and M["outside"] == 0, (M["holes_symmetric"], M["outside"])
    # a rounded outline pulls nothing out of symmetry
    r = 3.0
    arc = lambda cx, cy, a0: [[cx + r * math.cos(math.radians(a0 + 15 * k)), cy + r * math.sin(math.radians(a0 + 15 * k))] for k in range(7)]
    rounded = dict(ol, polygon=arc(177, 157, 0) + arc(103, 157, 90) + arc(103, 103, 180) + arc(177, 103, 270))
    s2 = holes.seats(4, rounded["bbox"], 5.5, rounded["polygon"], None, 4.6)
    assert s2 == seats[:4], s2
    # locked, board-only, kept and out-of-refs holes do not move; a hole already on a seat is not listed
    fixed = [_hole("P1", 130, 190, locked=True), _hole("H1", 141, 190, flags=["board_only"]), _hole("P2", 152, 190),
             _hole("P3", 105.5, 105.5)]
    snap = {"outline": ol, "footprints": list(fixed)}
    mv = holes.seat(snap, keep=["P2"])
    assert mv == [], mv
    mv = holes.seat({"outline": ol, "footprints": list(fixed)}, refs=["R1"])
    assert mv == [], mv
    mv = holes.seat({"outline": ol, "footprints": list(fixed)})
    assert [m["ref"] for m in mv] == ["P2"] and (mv[0]["x"], mv[0]["y"]) != (105.5, 105.5), mv
    # a seat under a part that stays put is skipped
    blocker = _fp("U9", 105.5, 105.5, w=12, h=12, locked=True)
    mv = holes.seat({"outline": ol, "footprints": [blocker, _hole("P2", 152, 190)]})
    assert mv and (mv[0]["x"], mv[0]["y"]) == (174.5, 105.5), mv
    assert holes.seat({"footprints": list(fps)}) == [], "no outline: nothing to seat on"


def test_auto_place_seats_schematic_holes_and_puts_the_dsub_on_an_edge():
    ol = _outline(90, 70)
    pads = [{"num": str(i + 1), "net": f"S{i}", "lx": -5.5 + 1.4 * i, "ly": 8.0, "w": 1.6, "h": 1.6, "type": "tht"} for i in range(9)]
    fps = [_fp("J1", 145, 135, w=31, h=20, pads=pads, footprint=DSUB)]       # dropped mid-board by sync
    fps += [_hole(f"P10{i}", 120 + 11 * i, 200) for i in range(1, 5)]
    fps += [_fp(f"R{i}", 110 + 3 * i, 210, pads=[{"num": "1", "net": f"S{i % 9}", "lx": -0.5, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"},
                                                 {"num": "2", "net": f"T{i}", "lx": 0.5, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"}])
            for i in range(12)]
    snap = {"outline": ol, "footprints": fps, "track_segs": []}
    res = elegant.auto_place(snap, candidates=1)
    by = {m["ref"]: m for m in res["moves"]}
    assert not res["unplaced"], res["unplaced"]
    assert "J1" in by and "J1" in res["roles"].get("connector", []), res["roles"]
    for i in range(1, 5):
        m = by[f"P10{i}"]
        assert min(m["x"] - 100, 190 - m["x"]) <= 6 and min(m["y"] - 100, 170 - m["y"]) <= 6, m
    assert res["notes"]["holes_seated"] == [m["ref"] for m in res["moves"][:4]]
    # the connector ends on an edge and nothing is outside (holes included)
    placed = [dict(d, **{k: by[d["ref"]][k] for k in ("x", "y", "rot")}) if d["ref"] in by else d for d in fps]
    M = elegance.placement_metrics([place.Part(d) for d in placed], ol)
    assert M["outside"] == 0 and M["holes_symmetric"] == 1.0, (M["outside"], M["holes_symmetric"])
    assert [c["ref"] for c in M["connectors"]] == ["J1"] and M["connectors"][0]["gap"] <= 1.5, M["connectors"]
    assert all(f["y"] in (135, 200, 210) for f in fps), "auto_place must not mutate the snapshot it was given"


def test_auto_size_uses_schematic_holes_and_adds_none():
    fps = [_hole(f"P10{i}", 120 + 11 * i, 200) for i in range(1, 5)]
    fps += [_fp(f"U{i}", 110 + 12 * i, 215, w=10, h=8, pads=[{"num": str(k), "net": f"A{(i + k) % 7}", "lx": -3 + k, "ly": 0, "w": 0.6, "h": 1.0, "type": "smd"}
                                                           for k in range(6)], footprint="Package_SO:SOIC-8") for i in range(6)]
    snap = {"footprints": fps, "outline": None}
    ops, info = setup_ext.size_ops(None, snap, holes="M3")
    assert ops is not None and info["schematic_holes"] == [f"P10{i}" for i in range(1, 5)], info
    assert [o["op"] for o in ops] == ["outline", "outline_prop"], "no board-only holes on top of the schematic's own"
    # without schematic holes the same call still adds the four corner holes
    comps = [d for d in fps if not holes.is_hole_fp(d)]

    class _Pr:      # size_ops only asks the project for reserved references and the hole library
        class sch:
            @staticmethod
            def exists():
                return False
    orig = board.fp_libs
    board.fp_libs = lambda pr: {}
    try:
        ops2, info2 = setup_ext.size_ops(_Pr, {"footprints": comps, "outline": None}, holes="M3")
    finally:
        board.fp_libs = orig
    assert [o["op"] for o in ops2] == ["outline", "outline_prop", "holes_multi"] and "schematic_holes" not in info2
    # the trial board reserved the corners for the schematic's holes: it is larger than the bare parts need
    assert info["width"] * info["height"] >= info2["width"] * info2["height"] * 0.95, (info, info2)


# ── the score ─────────────────────────────────────────────────────────────
def test_score_counts_holes_outside_bare_areas_and_scattered_twins():
    ol = _outline(80, 60)
    grid = [_fp(f"R{i}", 106 + 6 * (i % 12), 106 + 6 * (i // 12)) for i in range(96)]        # evenly spread
    M = elegance.placement_metrics([place.Part(d) for d in grid + [_hole("P1", 150, 190)]], ol)
    assert M["outside"] == 1, "a mounting hole beside the board is outside"
    assert elegance.score(dict(M, tracks=0, silk_refs=0, silk_hidden=0, silk_bad={}), {"errors": 0, "unconnected": 0})["gates"] == ["outside_outline:1"]
    M0 = elegance.placement_metrics([place.Part(d) for d in grid], ol)
    assert M0["largest_empty"] <= 0.05 and M0["repeat_uniform"] is None, M0["largest_empty"]
    # the same parts squeezed into the left half: every third of the height still holds parts, one half is bare
    left = [_fp(f"R{i}", 104 + 3 * (i % 12), 106 + 6 * (i // 12)) for i in range(96)]
    M1 = elegance.placement_metrics([place.Part(d) for d in left], ol)
    assert M1["largest_empty"] >= 0.40, M1["largest_empty"]
    s0 = elegance.placement_score([place.Part(d) for d in grid], ol, src="user")["parts"]["space"]
    s1 = elegance.placement_score([place.Part(d) for d in left], ol, src="user")["parts"]["space"]
    assert s1 < s0 - 0.25, (s0, s1)
    assert elegance.largest_empty([], ol) == 1.0 and elegance.largest_empty([[100, 100, 180, 160]], ol) == 0.0
    # an L-shaped board: the missing corner is not "empty board"
    lshape = dict(ol, polygon=[[100, 100], [180, 100], [180, 130], [140, 130], [140, 160], [100, 160]])
    assert elegance.largest_empty([], lshape) <= 0.72, elegance.largest_empty([], lshape)
    # eight identical pots: a 4 x 2 matrix at one rotation is uniform, the same eight scattered and turned are not
    matrix = [_pot(f"RV{i}", 112 + 16 * (i % 4), 115 + 20 * (i // 4)) for i in range(8)]
    messy = [_pot(f"RV{i}", 111 + 16.3 * (i % 4) + 1.7 * (i // 4), 113 + 21 * (i // 4) + 2.1 * (i % 4), rot=90 * (i % 3)) for i in range(8)]
    Mm = elegance.placement_metrics([place.Part(d) for d in matrix], ol)
    Ms = elegance.placement_metrics([place.Part(d) for d in messy], ol)
    assert Mm["repeat_uniform"] == 1.0 and Ms["repeat_uniform"] <= 0.25, (Mm["repeat_uniform"], Ms["repeat_uniform"])
    pm, ps = (elegance.placement_score([place.Part(d) for d in x], ol, src="user") for x in (matrix, messy))
    assert pm["score"] > ps["score"] + 3, (pm["score"], ps["score"])
    worst = elegance.score(dict(Ms, tracks=0, silk_refs=8, silk_hidden=0, silk_bad={}), None)["worst"]
    assert any(w["what"] == "identical_parts_irregular" and "pcb_place(moves=" in w["fix"] for w in worst), worst
    # three of a kind (three DIP-8s in three places) are not a group
    three = elegance.placement_metrics([place.Part(d) for d in messy[:3]], ol)
    assert three["repeat_uniform"] is None


def test_identical_large_parts_are_placed_as_one_matrix():
    from copper_kicad.pcb import arrays
    ol = _outline(110, 90)
    # sixteen pots wired in a chain plus a header: connectivity alone scatters and turns them
    pots = []
    for i in range(16):
        d = _pot(f"RV{i + 1}", 230 + 13 * (i % 8), 230 + 13 * (i // 8))
        d["pad_list"][0]["net"], d["pad_list"][2]["net"] = f"W{i}", f"W{i + 1}"
        d["pad_list"][1]["net"] = "GND"
        pots.append(d)
    hdr = _fp("J1", 230, 260, w=2.5, h=10.2, footprint="Connector_PinHeader_2.54mm:PinHeader_1x04",
              pads=[{"num": str(k + 1), "net": n, "lx": 0, "ly": -3.81 + 2.54 * k, "w": 1.7, "h": 1.7, "type": "tht"}
                    for k, n in enumerate(("W0", "W16", "GND", "VCC"))])
    snap = {"outline": ol, "footprints": pots + [hdr], "track_segs": []}
    res = elegant.auto_place(snap, candidates=1)
    assert not res["unplaced"], res["unplaced"]
    by = {m["ref"]: m for m in res["moves"]}
    rv = [by[f"RV{i + 1}"] for i in range(16)]
    assert len({m["rot"] for m in rv}) == 1, sorted({m["rot"] for m in rv})
    xs, ys = sorted({m["x"] for m in rv}), sorted({m["y"] for m in rv})
    assert len(xs) * len(ys) == 16 and len(xs) in (2, 4, 8), (xs, ys)           # a full matrix, no stragglers
    assert len({round(b - a, 3) for a, b in zip(xs, xs[1:])}) == 1 and len({round(b - a, 3) for a, b in zip(ys, ys[1:])}) <= 1, (xs, ys)
    arr = res["notes"]["arrays"]
    assert len(arr) == 1 and sorted(arr[0]["refs"]) == sorted(f"RV{i + 1}" for i in range(16)), arr
    placed = [dict(d, **{k: by[d["ref"]][k] for k in ("x", "y", "rot")}) for d in pots + [hdr]]
    parts = [place.Part(d) for d in placed]
    M = elegance.placement_metrics(parts, ol)
    assert M["repeat_uniform"] == 1.0 and M["outside"] == 0, M["repeat_uniform"]
    assert not place.overlaps({"footprints": placed}), place.overlaps({"footprints": placed})
    # three of a kind, small parts, connectors and locked parts are not groups
    pl = elegant.ElegantPlacer([place.Part(dict(d, locked=(i < 13))) for i, d in enumerate(placed)], ol)
    for p in pl.parts:
        p.placed = True
    assert arrays.groups_of(pl.parts) == [], "13 of 16 locked leaves three movable twins: not a group"
    # a board with no room for any block leaves the group exactly as the placer put it
    tight = _outline(40, 30)
    pl = elegant.ElegantPlacer([place.Part(d) for d in pots[:4]] + [place.Part(_fp("U1", 120, 115, w=14, h=28, locked=True))], tight)
    spots = [(106.5, 106.5, 0), (133.5, 106.5, 90), (106.5, 123.5, 180), (133.5, 123.5, 270)]
    for p, (x, y, r) in zip(pl.parts[:4], spots):
        pl.commit(p, x, y, r)
    assert arrays.regularise(pl) == [], pl.notes
    assert [(p.x, p.y, p.rot) for p in pl.parts[:4]] == spots and all(p.placed for p in pl.parts)


def test_courtyard_estimate_never_gates_a_board_drc_calls_clean():
    ol = _outline()
    M = elegance.placement_metrics([place.Part(_fp(f"R{i}", 106 + 4 * (i % 12), 106 + 5 * (i // 12))) for i in range(36)], ol)
    M.update({"tracks": 0, "silk_refs": 36, "silk_hidden": 0, "silk_bad": {}, "courtyard_overlaps": 207, "src": "user"})
    clean = elegance.score(M, {"errors": 0, "unconnected": 0})
    assert clean["gates"] == [] and clean["score"] == clean["ungated"], clean["gates"]
    assert elegance.score(M, {"errors": 2, "unconnected": 0})["gates"] == ["drc_errors:2"]
    assert elegance.score(M, None)["gates"] == ["courtyard_overlap:207"], "no DRC result: the estimate is all there is"


# ── outline import ────────────────────────────────────────────────────────
def test_outline_that_crosses_itself_is_refused_with_the_place():
    d = _TMP / "cad"
    d.mkdir(parents=True, exist_ok=True)
    bow = d / "bowtie.svg"
    bow.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 40" width="80mm" height="40mm">'
                   '<path d="M0 0 L80 0 L0 40 L80 40 Z"/></svg>', encoding="utf-8")
    try:
        cadout.load(bow)
        raise AssertionError("a bow-tie outline was accepted")
    except cadout.OutlineError as e:
        assert e.code == "OUTLINE_SELF_INTERSECTS" and e.data["at"] == [40.0, 20.0], (e.code, e.message, e.data)
    # figure of eight drawn with a DXF polyline
    dxf = d / "eight.dxf"
    pts = [(0, 0), (30, 20), (60, 0), (60, 20), (30, 0), (0, 20)]
    body = "0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n8\n0\n90\n%d\n70\n1\n" % len(pts) + "".join(f"10\n{x}\n20\n{y}\n" for x, y in pts)
    dxf.write_text(body + "0\nENDSEC\n0\nEOF\n", encoding="utf-8")
    try:
        cadout.load(dxf, units="mm")
        raise AssertionError("a figure-of-eight outline was accepted")
    except cadout.OutlineError as e:
        assert e.code == "OUTLINE_SELF_INTERSECTS", (e.code, e.message)
    # clean outlines are untouched, and the check is quick on a long polyline
    ok = d / "ok.svg"
    ok.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 40" width="80mm" height="40mm">'
                  '<path d="M5 0 L75 0 A5 5 0 0 1 80 5 L80 35 A5 5 0 0 1 75 40 L5 40 A5 5 0 0 1 0 35 L0 5 A5 5 0 0 1 5 0 Z"/>'
                  '<circle cx="40" cy="20" r="6"/></svg>', encoding="utf-8")
    ol, rep = cadout.load(ok)
    assert ol["size"] == [80.0, 40.0] and len(ol["cutouts"]) == 1, (ol["size"], len(ol["cutouts"]))
    ring = [(50 * math.cos(2 * math.pi * k / 4000), 50 * math.sin(2 * math.pi * k / 4000)) for k in range(4000)]
    t = time.time()
    assert cadout._crossing(ring) is None
    assert time.time() - t < 2.0, f"self-intersection check took {time.time() - t:.1f} s on 4000 points"
    star = [(0, 0), (10, 0), (10, 10), (4, -3), (0, 10)]
    assert cadout._crossing(star) is not None
    assert cadout._crossing([(0, 0), (10, 0), (10, 10)]) is None


def test_truncated_step_says_so():
    import outline_fixtures as F
    d = _TMP / "cad"
    d.mkdir(parents=True, exist_ok=True)
    full = d / "tray.step"
    F.enclosure(full)
    text = full.read_text(encoding="utf-8", errors="replace")
    cut = d / "cut.step"
    # keep the header and the tail (the faces), drop a slice of entities in the middle
    lines = text.splitlines(keepends=True)
    data0 = next(i for i, ln in enumerate(lines) if ln.strip().startswith("DATA"))
    ents = [i for i in range(data0 + 1, len(lines)) if lines[i].lstrip().startswith("#")]
    drop = set(ents[len(ents) // 4: len(ents) // 2])
    cut.write_text("".join(ln for i, ln in enumerate(lines) if i not in drop), encoding="utf-8")
    try:
        cadout.load(cut)
        raise AssertionError("a STEP file with a quarter of its entities missing was read without complaint")
    except cadout.OutlineError as e:
        assert e.code == "OUTLINE_PARSE_FAILED" and "KeyError" not in e.message, (e.code, e.message)
        assert "incomplete" in e.message and "export the file again" in (e.fix or ""), (e.message, e.fix)


def _skip(why):
    print(f"  (skipped: {why})")


# ── through the tools, on KiCad's own demo project ────────────────────────
def _demo(name):
    from copper_kicad.kicad import env
    try:
        inst = env.kicad()
    except Exception:  # noqa: BLE001
        return None
    roots = ([Path(inst.share)] if inst.share else []) + [Path(inst.root) / "share" / "kicad", Path("/usr/share/kicad")]
    for cand in [r / "demos" / name for r in roots]:
        if cand.is_dir():
            return cand
    return None


def test_pic_programmer_demo_from_schematic_keeps_its_holes_and_connector_on_the_board():
    src = _demo("pic_programmer")
    if src is None:
        return _skip("KiCad demo projects not installed")
    d = _TMP / "pic é"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(src, d)
    (d / "pic_programmer.kicad_pcb").unlink()
    call("pcb_sync", project=str(d), render=False)
    setup, _ = call("pcb_setup", project=str(d), auto_size=True, holes="M3", render=False)
    assert any("schematic already has 6 mounting hole" in c for c in setup["changed"]), setup["changed"]
    data, _ = call("pcb_place", project=str(d), auto=True, render=False)
    assert not [f for f in data["findings"] if f["code"] in ("OUTSIDE_OUTLINE", "DID_NOT_FIT")], data["findings"][:3]
    assert "J1" in data["auto"]["roles"]["connector"], data["auto"]["roles"]
    pr = projmod.resolve(str(d))
    snap = board.read(pr, what=["footprints"])
    x0, y0, x1, y1 = snap["outline"]["bbox"]
    hole_fps = [f for f in snap["footprints"] if holes.is_hole(f["footprint"])]
    assert sorted(f["ref"] for f in hole_fps) == [f"P10{i}" for i in range(1, 7)], [f["ref"] for f in hole_fps]
    assert all(x0 < f["x"] < x1 and y0 < f["y"] < y1 for f in hole_fps), [(f["ref"], f["x"], f["y"]) for f in hole_fps]
    e = elegance.elegance(pr, metrics=True)
    M = e["metrics"]
    assert M["outside"] == 0 and M["holes"] == 6 and M["holes_symmetric"] == 1.0, (M["outside"], M["holes"], M["holes_symmetric"])
    j1 = next(c for c in M["connectors"] if c["ref"] == "J1")
    assert j1["ok"], j1
    assert not any(g.startswith(("outside_outline", "courtyard_overlap")) for g in e["gates"]), e["gates"]


def test_clean_demo_board_is_not_gated_by_the_courtyard_estimate():
    src = _demo("pic_programmer")
    if src is None:
        return _skip("KiCad demo projects not installed")
    d = _TMP / "pic-routed"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(src, d)
    pr = projmod.resolve(str(d))
    before = (d / "pic_programmer.kicad_pcb").read_bytes()
    drc = board.drc(pr, parity=False, record=False)
    e = elegance.elegance(pr, drc=drc, metrics=True)
    assert (d / "pic_programmer.kicad_pcb").read_bytes() == before, "scoring must not write the board"
    if drc["errors"] == 0 and drc["unconnected"] == 0:
        assert e["gates"] == [] and e["score"] == e["ungated"], (e["gates"], e["metrics"]["courtyard_overlaps"])
    # the DB9 on this board is a connector for the score too
    assert "J1" in [c["ref"] for c in e["metrics"]["connectors"]], e["metrics"]["connectors"]


def test_stitching_vias_respect_the_boards_minimum_ring():
    import test_pcb as T
    d = _TMP / "stitch"
    if d.exists():
        shutil.rmtree(d)
    T.build(d)
    call("pcb_sync", project=str(d), render=False)
    call("pcb_setup", project=str(d), width=50, height=40, render=False)
    call("pcb_place", project=str(d), auto=True, render=False)
    pr = projmod.resolve(str(d))
    pro = json.loads(pr.pro.read_text(encoding="utf-8"))
    rules = pro["board"]["design_settings"]["rules"]
    rules["min_via_annular_width"] = 0.3            # an older project's rule: 0.6 / 0.3 mm vias have a 0.15 mm ring
    rules["min_via_diameter"] = 0.6
    pr.pro.write_text(json.dumps(pro, indent=2), encoding="utf-8")
    pour, _ = call("pcb_pour", project=str(d), net="GND", stitch=True, render=False)
    assert any("stitching vias" in c and not c.startswith("0 ") for c in pour["changed"]), pour["changed"]
    drc = board.drc(pr, parity=False, record=False)
    ring = {k: v for k, v in drc["by_type"].items() if "annular" in k}
    assert not ring, f"stitching vias violate the board's minimum annular ring: {ring}"
    vias = [t for t in board.read(pr, what=["tracks"])["track_list"] if t["type"] == "via"]
    assert vias and all(v.get("size", 0.9) >= 0.9 - 1e-6 for v in vias if "size" in v), vias[:2]


if __name__ == "__main__":
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
    print(f"temp dir: {_TMP}")
    sys.exit(1 if fails else 0)
