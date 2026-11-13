"""Outline import tests: STEP / STEP-Z / DXF / SVG / STL -> Edge.Cuts through pcb_setup(outline_file=...).

Run:  python tests/test_outline.py [test_name ...]      (KiCad's python; needs KiCad 9/10)
Slow corpus tests over the installed 3D library run with COPPER_TEST_SLOW=1.
Fixtures are generated (tests/outline_fixtures.py); nothing is downloaded.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Copper's state (active project, recent list) must never land in the real ~/.copper: project.STATE_DIR is
# fixed when copper_kicad is first imported, so this has to come before that import.
__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad.cad import dxf as cdxf, ir, outline as cadout, pick as cpick, step as cstep  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
import copper_kicad.tools.pcb_tools  # noqa: E402,F401 — registers the tools
from copper_kicad.pcb import board  # noqa: E402
from copper_kicad import project as projmod  # noqa: E402
import outline_fixtures as fx  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="copper-test-outline-"))
_CACHE: dict = {}


def call(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    if r["isError"]:
        raise AssertionError(f"{name} failed: {r['content'][0]['text'][:900]}")
    return r.get("structuredContent") or {}, r


def call_err(name, **args):
    """-> {"code", "message", "fix"} of a call that must fail."""
    r = registry.call_tool(name, args, lambda f, m: None)
    assert r["isError"], f"{name} was expected to fail"
    sc = r["structuredContent"]
    return {"error": {"code": sc.get("error"), "message": sc.get("message", ""), "fix": sc.get("fix", "") or ""}}


def _images(r):
    return [c for c in r["content"] if c["type"] == "image"]


def _codes(d):
    return [f["code"] for f in d.get("findings", [])]


def _project(name):
    """A synced project (schematic + board with the parts beside it), from the PCB test fixture."""
    import test_pcb as T
    d = _TMP / name
    if d.exists():
        shutil.rmtree(d)
    T.build(d)
    call("pcb_sync", project=str(d), render=False)
    return str(d)


def _pcb(d):
    return projmod.resolve(d).pcb


def _fancy():
    """The fancy board and its exports, made once: {'pcb', 'step', 'stpz', 'dxf', 'svg', 'stl', 'holes'}"""
    if "fancy" in _CACHE:
        return _CACHE["fancy"]
    d = _TMP / "fancy src"
    d.mkdir(parents=True, exist_ok=True)
    pcb = d / "fancy.kicad_pcb"
    holes = fx.fancy_board(pcb)
    out = {"pcb": pcb, "holes": holes}
    env.run(["pcb", "export", "step", "--board-only", "--force", "-o", str(d / "fancy.step"), str(pcb)], timeout=180)
    env.run(["pcb", "export", "stpz", "--board-only", "--force", "-o", str(d / "fancy.stpz"), str(pcb)], timeout=180)
    env.run(["pcb", "export", "stl", "--board-only", "--force", "-o", str(d / "fancy.stl"), str(pcb)], timeout=180)
    env.run(["pcb", "export", "dxf", "--layers", "Edge.Cuts", "--output-units", "mm", "--mode-single", "-o", str(d / "fancy.dxf"), str(pcb)], timeout=180)
    env.run(["pcb", "export", "svg", "--layers", "Edge.Cuts", "--exclude-drawing-sheet", "--page-size-mode", "2", "--mode-single",
             "-o", str(d / "fancy.svg"), str(pcb)], timeout=180)
    for k in ("step", "stpz", "stl", "dxf", "svg"):
        p = d / f"fancy.{k}"
        assert p.is_file(), f"kicad-cli did not write {p.name}"
        out[k] = p
    _CACHE["fancy"] = out
    return out


def _roundtrip(fmt, tol_xor, tol_dev, max_shapes=None, thickness=None):
    f = _fancy()
    d = _project(f"rt {fmt}")
    nets = lambda: [(n["name"], n["nodes"]) for n in call("pcb_read", project=d, what=["nets"])[0]["nets"]]
    nets0 = nets()
    t0 = time.time()
    # KiCad plots an SVG relative to the board area, so only the 3D / DXF exports keep absolute coordinates
    absolute = fmt != "svg"
    data, r = call("pcb_setup", project=d, outline_file=str(f[fmt]), outline_keep_position=absolute, origin=[100, 100], render=False)
    dt = time.time() - t0
    oi = data["outline_import"]
    xor, area, dev = fx.compare(f["pcb"], _pcb(d), holes=[(x, y, 1.6) for x, y in f["holes"]])
    assert xor <= tol_xor * area, f"{fmt}: XOR {xor:.4f} mm2 of {area:.1f} ({100 * xor / area:.4f} %)"
    assert dev <= tol_dev, f"{fmt}: max deviation {dev:.4f} mm"
    assert oi["cutouts"] == 2, oi
    got = sorted((round(h["at"][0], 2), round(h["at"][1], 2)) for h in oi["holes"] if isinstance(h, dict))
    assert len(got) == 4 and all(any(abs(g[0] - x) <= 0.01 and abs(g[1] - y) <= 0.01 for g in got) for x, y in f["holes"]), got
    assert all(h["size"] == "M3" for h in oi["holes"] if isinstance(h, dict))
    if max_shapes:
        n = sum(oi["shapes"].values())
        assert n <= max_shapes, f"{fmt}: {n} edge shapes (arcs not recovered?)"
    assert oi["shapes"]["arcs"] >= 6, oi["shapes"]
    if thickness:
        assert oi["thickness"] == thickness, oi
        assert "BOARD_THICKNESS_DIFFERS" in _codes(data), _codes(data)
        assert "(thickness 1.6)" in _pcb(d).read_text(encoding="utf-8")[:20000], "the board thickness must not be written"
    assert nets() == nets0, "an outline import must not touch the netlist"
    # A15: what the import adds stays small (the rules block of a first-time setup is the existing 1 kB)
    assert len(json.dumps(oi)) <= 700, f"outline_import is {len(json.dumps(oi))} bytes"
    assert len(json.dumps({k: v for k, v in data.items() if k != "rules"})) <= 2500, len(json.dumps(data))
    return data, xor, area, dev, dt


# ── A16: unit tests of the shared geometry ────────────────────────────────
def test_ir_chain_simplify_classify():
    circ = [(10 + 5 * math.cos(2 * math.pi * k / 96), 20 + 5 * math.sin(2 * math.pi * k / 96)) for k in range(96)]
    prims = [("L", circ[k], circ[(k + 1) % 96]) for k in range(96)]
    random.Random(1).shuffle(prims)
    loops, opens = ir.chain(prims)
    assert len(loops) == 1 and not opens
    s = ir.simplify(loops[0])
    assert len(s) == 1 and s[0][0] == "C" and abs(s[0][2] - 5) < 0.01 and math.dist(s[0][1], (10, 20)) < 0.01, s
    # rounded rectangle from 200 points -> 4 lines + 4 arcs
    pts = []
    w, h, r = 40.0, 30.0, 4.0
    for cx, cy, a0 in ((w - r, r, -90), (w - r, h - r, 0), (r, h - r, 90), (r, r, 180)):
        pts += [(cx + r * math.cos(math.radians(a0 + 90 * k / 49)), cy + r * math.sin(math.radians(a0 + 90 * k / 49))) for k in range(50)]
    s = ir.simplify([("L", pts[k], pts[(k + 1) % len(pts)]) for k in range(len(pts))])
    kd = ir.kinds(s)
    assert kd == {"lines": 4, "arcs": 4, "circles": 0}, kd
    # a 90 degree corner is never turned into an arc
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    s = ir.simplify([("L", sq[k], sq[(k + 1) % 4]) for k in range(4)])
    assert ir.kinds(s) == {"lines": 4, "arcs": 0, "circles": 0}, s
    fine = [(x / 10, 0) for x in range(100)] + [(10, y / 10) for y in range(100)] + [(10 - x / 10, 10) for x in range(100)] + [(0, 10 - y / 10) for y in range(100)]
    s = ir.simplify([("L", fine[k], fine[(k + 1) % len(fine)]) for k in range(len(fine))])
    assert ir.kinds(s) == {"lines": 4, "arcs": 0, "circles": 0}, ir.kinds(s)
    # classify: outer, mounting hole, drill, cutout, loop outside
    outer = [("L", sq[k], sq[(k + 1) % 4]) for k in range(4)]
    o2 = ir.transform(outer, lambda p: (p[0] * 5, p[1] * 5))
    ol = ir.classify([o2, [("C", (5, 5), 1.6)], [("C", (10, 5), 0.4)], ir.transform(outer, lambda p: (p[0] + 20, p[1] + 20)),
                      ir.transform(outer, lambda p: (p[0] + 100, p[1]))])
    assert len(ol["holes"]) == 1 and ol["holes"][0]["size"] == "M3" and ol["ignored"]["drills"] == 1
    assert len(ol["cutouts"]) == 1 and ol["ignored"]["other_loops"] == 1
    assert abs(ol["area"] - (2500 - math.pi * 1.6 ** 2 - 100)) < 0.2, ol["area"]
    assert len(ir.classify([o2, [("C", (10, 5), 0.4)]], holes="all")["cutouts"]) == 1
    # two arcs of one corner merge; the polygon decimator keeps the shape
    a1 = ir.arc_from((0, 0), 5, 0, math.pi / 4)
    a2 = ir.arc_from((0, 0), 5, math.pi / 4, math.pi / 2)
    m = ir.merge_arcs([a1, a2])
    assert len(m) == 1 and math.dist(m[0][3], (0, 5)) < 1e-9 and abs(math.hypot(*m[0][2]) - 5) < 1e-9
    dec = ir.decimate([list(p) for p in circ], 0.05)
    assert 8 <= len(dec) < 60 and abs(abs(ir.poly_area(dec)) - math.pi * 25) < 1.5, len(dec)


def test_dxf_bulge_mirrored_arc_insert_and_units():
    d = _TMP / "dxf"
    d.mkdir(exist_ok=True)

    def w(name, body, header=""):
        p = d / name
        p.write_text("0\nSECTION\n2\nHEADER\n" + header + "0\nENDSEC\n" + body + "0\nEOF\n", encoding="ascii")
        return p
    # LWPOLYLINE 1 x 0.5 inch with one bulge corner pair (a D shape): units inch -> 25.4 mm wide
    lw = ("0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n8\nOUTLINE\n90\n4\n70\n1\n10\n0\n20\n0\n10\n1\n20\n0\n42\n1\n10\n1\n20\n0.5\n10\n0\n20\n0.5\n"
          "0\nCIRCLE\n8\nOUTLINE\n10\n0.25\n20\n0.25\n40\n0.063\n0\nENDSEC\n")
    p = w("inch.dxf", lw, "9\n$INSUNITS\n70\n1\n")
    ol, rep = cadout.load(p)
    assert abs(ol["size"][0] - (25.4 + 6.35)) < 0.01 and abs(ol["size"][1] - 12.7) < 0.01, ol["size"]   # bulge 1 = half circle r 6.35
    assert ir.kinds(ol["outer"]) == {"lines": 3, "arcs": 1, "circles": 0}
    assert len(ol["holes"]) == 1 and ol["holes"][0]["size"] == "M3" and "units_assumed" not in rep
    # the same file without $INSUNITS is read as mm and says so; too small then
    p = w("nounits.dxf", lw)
    try:
        cadout.load(p)
        raise AssertionError("expected OUTLINE_TOO_SMALL")
    except cadout.OutlineError as e:
        assert e.code == "OUTLINE_TOO_SMALL" and "outline_units" in e.fix
    ol, rep = cadout.load(p, units="inch")
    assert abs(ol["size"][1] - 12.7) < 0.01
    # mirrored ARC (extrusion 0,0,-1) closes a shape with three lines
    arc = ("0\nSECTION\n2\nENTITIES\n0\nLINE\n8\n0\n10\n0\n20\n0\n11\n20\n21\n0\n0\nLINE\n8\n0\n10\n20\n20\n0\n11\n20\n21\n10\n"
           "0\nLINE\n8\n0\n10\n0\n20\n0\n11\n0\n21\n10\n"
           "0\nARC\n8\n0\n10\n-10\n20\n10\n40\n10\n50\n0\n51\n180\n230\n-1\n0\nENDSEC\n")
    ol, rep = cadout.load(w("mirror.dxf", arc, "9\n$INSUNITS\n70\n4\n"))
    assert ir.kinds(ol["outer"]) == {"lines": 3, "arcs": 1, "circles": 0} and abs(ol["size"][1] - 20) < 0.01, (ol["size"], ir.kinds(ol["outer"]))
    # INSERT of a block (a 30 x 20 rectangle) moved and rotated by 90 degrees
    blk = ("0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n8\n0\n2\nBRD\n10\n0\n20\n0\n0\nLWPOLYLINE\n8\n0\n90\n4\n70\n1\n10\n0\n20\n0\n10\n30\n20\n0\n10\n30\n20\n20\n10\n0\n20\n20\n"
           "0\nENDBLK\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nINSERT\n8\nBoard_Outline\n2\nBRD\n10\n50\n20\n50\n50\n90\n0\nENDSEC\n")
    ol, rep = cadout.load(w("insert.dxf", blk, "9\n$INSUNITS\n70\n4\n"))
    assert abs(ol["size"][0] - 20) < 0.01 and abs(ol["size"][1] - 30) < 0.01, ol["size"]
    # binary DXF is named explicitly
    b = d / "bin.dxf"
    b.write_bytes(b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 64)
    try:
        cadout.load(b)
        raise AssertionError("expected OUTLINE_FORMAT_UNSUPPORTED")
    except cadout.OutlineError as e:
        assert e.code == "OUTLINE_FORMAT_UNSUPPORTED"
    for name in ("x.igs", "x.3mf", "x.dwg"):
        q = d / name
        q.write_text("x")
        try:
            cadout.load(q)
            raise AssertionError("expected OUTLINE_FORMAT_UNSUPPORTED")
        except cadout.OutlineError as e:
            assert e.code == "OUTLINE_FORMAT_UNSUPPORTED" and ".step" in e.message


def test_svg_native_arcs_and_units():
    d = _TMP / "svg"
    d.mkdir(exist_ok=True)
    p = d / "board.svg"
    # 50 x 30 mm, r=5 corners as path arcs, a circle, inside a translated + scaled group (user unit = 0.5 mm)
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="40mm" viewBox="0 0 120 80">'
                 '<g id="edge" transform="translate(10,10) scale(2)">'
                 '<path d="M5,0 H45 A5,5 0 0 1 50,5 V25 A5,5 0 0 1 45,30 H5 A5,5 0 0 1 0,25 V5 A5,5 0 0 1 5,0 Z"/>'
                 '<circle cx="10" cy="10" r="1.6"/><rect x="20" y="10" width="10" height="6" rx="1"/></g>'
                 '<g id="notes"><rect x="0" y="0" width="120" height="80"/></g></svg>', encoding="utf-8")
    ol, rep = cadout.load(p, layer="edge")
    assert ol["size"] == [50.0, 30.0], ol["size"]
    assert ir.kinds(ol["outer"]) == {"lines": 4, "arcs": 4, "circles": 0}, ir.kinds(ol["outer"])
    r = ir.circle3(*[q for q in ol["outer"] if q[0] == "A"][0][1:4])[1]
    assert abs(r - 5.0) < 1e-6, r
    assert len(ol["holes"]) == 1 and len(ol["cutouts"]) == 1 and ir.kinds(ol["cutouts"][0])["arcs"] == 4
    ol2, _ = cadout.load(p)           # without the layer the 60 x 40 frame of the other group is the largest loop
    assert ol2["size"] == [60.0, 40.0]
    try:
        cadout.load(p, layer="nope")
        raise AssertionError("expected an error naming the groups")
    except cadout.OutlineError as e:
        assert "edge" in e.message
    # a cubic Bezier circle approximation is recovered as arcs within 0.02 mm
    k = 0.5522847498 * 10
    q = d / "bez.svg"
    q.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="30mm" height="30mm" viewBox="0 0 30 30"><path d="'
                 f'M25,15 C25,{15 + k} {15 + k},25 15,25 C{15 - k},25 5,{15 + k} 5,15 C5,{15 - k} {15 - k},5 15,5 '
                 f'C{15 + k},5 25,{15 - k} 25,15 Z"/></svg>', encoding="utf-8")
    ol, rep = cadout.load(q)
    assert abs(ol["size"][0] - 20) < 0.02 and sum(ir.kinds(ol["outer"]).values()) <= 8, (ol["size"], ir.kinds(ol["outer"]))
    assert "curves_approximated" in rep["notes"]


# ── A20 / A23: STEP syntax and size ───────────────────────────────────────
def test_step_syntax_variants_parse_alike():
    d = _TMP / "syntax"
    d.mkdir(exist_ok=True)
    base = d / "enc.step"
    fx.enclosure(base)
    ref, _ = cadout.load(base)
    txt = base.read_text(encoding="utf-8")

    def same(path, **kw):
        ol, rep = cadout.load(path, **kw)
        assert ol["size"] == ref["size"] and len(ol["holes"]) == 4 and len(ol["cutouts"]) == 1, (path.name, ol["size"])
    a = d / "data in header.step"
    fx.enclosure(a, header_extra=" DATA; trap")
    same(a)
    b = d / "edition3.step"
    fx.enclosure(b, data_kw="DATA('x',('AUTOMOTIVE_DESIGN'));")
    same(b)
    c = d / "bom.step"
    c.write_bytes(b"\xef\xbb\xbf" + txt.encode("utf-8"))
    same(c)
    u = d / "UPPER.STEP"
    u.write_text(txt, encoding="utf-8")
    same(u)
    sp = d / "späce dir"
    sp.mkdir(exist_ok=True)
    s2 = sp / "gehäuse v2.step"
    s2.write_text(txt.replace("DATA;", "/* a 'comment' */ DATA;").replace("ENDSEC;\nEND", "/* tail */ENDSEC;\nEND"), encoding="utf-8")
    same(s2)
    z = d / "enc.stpz"
    z.write_bytes(gzip.compress(txt.encode("utf-8")))
    same(z)
    import zipfile
    zz = d / "enc-zip.stpz"
    with zipfile.ZipFile(zz, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("enc.step", txt)
    same(zz)
    bad = d / "not.step"
    bad.write_text("hello")
    try:
        cadout.load(bad)
        raise AssertionError("expected OUTLINE_PARSE_FAILED")
    except cadout.OutlineError as e:
        assert e.code == "OUTLINE_PARSE_FAILED"
    # kicad-cli's own .stpz (a gzip stream) gives the same outline as its .step
    f = _fancy()
    o1, _ = cadout.load(f["step"])
    o2, _ = cadout.load(f["stpz"])
    assert o1["size"] == o2["size"] and ir.kinds(o1["outer"]) == ir.kinds(o2["outer"])


def test_file_too_large_is_refused_before_parsing():
    p = _TMP / "huge.step"
    with open(p, "wb") as f:
        f.write(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n")
        chunk = b"#1 = CARTESIAN_POINT('',(0.,0.,0.));\n" * 20000
        for _ in range(64):
            f.write(chunk)
    assert p.stat().st_size > 45 * 1024 * 1024
    t = time.time()
    try:
        cadout.load(p)
        raise AssertionError("expected OUTLINE_FILE_TOO_LARGE")
    except cadout.OutlineError as e:
        assert e.code == "OUTLINE_FILE_TOO_LARGE" and "board" in e.fix
    assert time.time() - t < 0.5
    p.unlink()


# ── A8 / A9 / A17 / A18: enclosures and assemblies ────────────────────────
def test_enclosure_mm_inch_and_hard_cases():
    d = _TMP / "enc"
    d.mkdir(exist_ok=True)
    fx.enclosure(d / "mm.step")
    fx.enclosure(d / "inch.step", unit="inch")
    res = {}
    for n in ("mm", "inch"):
        ol, rep = cadout.load(d / f"{n}.step")
        assert rep["mode"] == "enclosure" and rep["ambiguous"], rep       # auto-picked enclosures are never "confident"
        assert rep["candidates"] and rep["clearance"] == 0.5
        assert ol["size"] == [75.0, 45.0], ol["size"]
        assert sorted(h["size"] for h in ol["holes"]) == ["M3"] * 4 and len(ol["cutouts"]) == 1
        e = ol["enclosure"]
        assert e["cavity_depth"] == 18.0 and e["seat_height"] == 4.0 and e["max_height_top"] == 12.4 and e["max_height_bottom"] == 4.0, e
        res[n] = sorted((round(h["x"], 2), round(h["y"], 2)) for h in ol["holes"])
    assert all(abs(a[0] - b[0]) <= 0.01 and abs(a[1] - b[1]) <= 0.01 for a, b in zip(res["mm"], res["inch"]))
    # the caller saying "enclosure" is a confident pick
    ol, rep = cadout.load(d / "mm.step", mode="enclosure")
    assert not rep["ambiguous"], rep["why"]
    for name, zh, flags, win in (("e-lid", 20, "walls lid", 0), ("e-window", 20, "walls window", 1), ("e-tall", 70, "walls", 0),
                                 ("e-tall-lid", 70, "walls lid", 0)):
        fx.enclosure(d / f"{name}.step", zh=zh, flags=flags)
        ol, rep = cadout.load(d / f"{name}.step", clearance=0)
        assert rep["mode"] == "enclosure" and ol["size"] == [76.0, 46.0], (name, rep["mode"], ol["size"])
        assert rep["up"] == "+z", (name, rep["up"])
        assert ol["ignored"].get("windows", 0) == win and len(ol["cutouts"]) == 1, (name, ol["ignored"], len(ol["cutouts"]))
        if "lid" in flags:
            assert any(c["kind"].startswith("board") for c in rep["candidates"]), rep["candidates"]   # the lid is offered
    # forcing board mode on the lid file takes the lid; picking a candidate id works too
    ol, rep = cadout.load(d / "e-lid.step", mode="board")
    assert ol["size"] == [80.0, 50.0] and rep["mode"] == "board" and ol["thickness"] == 2.0
    cid = next(c["id"] for c in cadout.load(d / "e-lid.step")[1]["candidates"] if c["kind"] == "cavity floor")
    ol, rep = cadout.load(d / "e-lid.step", pick=cid, clearance=0)
    assert ol["size"] == [76.0, 46.0]


def test_assembly_transforms_place_lid_and_pcb():
    d = _TMP / "asm"
    d.mkdir(exist_ok=True)
    fx.enclosure(d / "tray-lid.step", flags="walls asm")
    ol, rep = cadout.load(d / "tray-lid.step")
    assert rep["mode"] == "enclosure" and ol["size"] == [75.0, 45.0] and rep["ambiguous"], (rep["mode"], ol["size"], rep["why"])
    fx.enclosure(d / "tray-lid-pcb.step", flags="walls asm pcb")
    ol, rep = cadout.load(d / "tray-lid-pcb.step", keep_position=True)
    # the PCB (60 x 40 x 1.6, modelled at its own origin, placed at (10, 5, 6)) sits inside the cavity -> board mode
    assert rep["mode"] == "board" and ol["size"] == [60.0, 40.0] and ol["thickness"] == 1.6, (rep["mode"], ol["size"], rep["why"])
    b = ol["bbox"]
    assert abs(b[0] - 10) <= 0.01 and abs(b[2] - 70) <= 0.01 and abs(-b[3] - 5) <= 0.01 and abs(-b[1] - 45) <= 0.01, b
    assert not rep["ambiguous"], rep["why"]
    # without the transform the lid would sit at z = 0..2 inside the tray floor: check it really moved
    m, g, faces = cstep.candidates(str(d / "tray-lid.step"))
    lid_levels = sorted({round(f.d * (1 if f.n[2] > 0 else -1), 3) for f in faces if abs(f.n[2]) > 0.99 and f.gross > 3900})
    assert 20.0 in lid_levels and 22.0 in lid_levels, lid_levels
    assert g.unresolved == 0


def test_real_assembly_export_with_components():
    """kicad-cli's STEP export with component models is a real assembly (one transform per part)."""
    import test_pcb as T
    d = _TMP / "asmreal"
    if d.exists():
        shutil.rmtree(d)
    T.build(d)
    call("pcb_sync", project=str(d), render=False)
    call("pcb_setup", project=str(d), width=45, height=32, radius=3, holes="M3", render=False)
    call("pcb_place", project=str(d), auto=True, render=False)
    pcb = _pcb(str(d))
    out = d / "full.step"
    env.run(["pcb", "export", "step", "--force", "--subst-models", "-o", str(out), str(pcb)], timeout=300)
    if not out.is_file():
        print("  (skipped: kicad-cli wrote no STEP with models)")
        return
    t = time.time()
    ol, rep = cadout.load(out)
    dt = time.time() - t
    assert rep["mode"] == "board" and ol["size"] == [45.0, 32.0] and ol["thickness"] == 1.6, (rep["mode"], ol["size"], ol["thickness"])
    assert len(ol["holes"]) == 4 and all(h["size"] == "M3" for h in ol["holes"]), ol["holes"]
    assert ol["ignored"]["drills"] >= 4 and dt < 4.0, (ol["ignored"], dt)
    m = cstep.load(str(out))
    g = cstep.Geo(m)
    st = cstep.solid_transforms(m, g)
    assert g.unresolved == 0
    if len(set(cstep.solids_of(m).values())) > 1:
        assert st, "component solids of an assembly must carry a placement"


# ── A1-A5: round trips through the tool ───────────────────────────────────
def test_step_roundtrip_through_pcb_setup():
    data, xor, area, dev, dt = _roundtrip("step", 0.0005, 0.02, thickness=1.2)
    print(f"  STEP: xor {xor:.4f} mm2 ({100 * xor / area:.4f} %), maxdev {dev:.4f} mm, {dt:.1f} s")


def test_dxf_roundtrip_through_pcb_setup():
    data, xor, area, dev, dt = _roundtrip("dxf", 0.0005, 0.02)
    print(f"  DXF: xor {xor:.4f} mm2 ({100 * xor / area:.4f} %), maxdev {dev:.4f} mm")


def test_stl_roundtrip_through_pcb_setup():
    data, xor, area, dev, dt = _roundtrip("stl", 0.001, 0.06, max_shapes=40)
    assert "OUTLINE_UNITS_ASSUMED" in _codes(data)
    print(f"  STL: xor {xor:.4f} mm2 ({100 * xor / area:.4f} %), maxdev {dev:.4f} mm, {sum(data['outline_import']['shapes'].values())} shapes")


def test_svg_roundtrip_through_pcb_setup():
    data, xor, area, dev, dt = _roundtrip("svg", 0.0005, 0.02)
    print(f"  SVG: xor {xor:.4f} mm2 ({100 * xor / area:.4f} %), maxdev {dev:.4f} mm")


def test_rounded_rect_is_exactly_four_lines_and_four_arcs():
    d = _TMP / "rr src"
    d.mkdir(exist_ok=True)
    src = d / "rr.kicad_pcb"
    fx.rounded_board(src)
    env.run(["pcb", "export", "step", "--board-only", "--force", "-o", str(d / "rr.step"), str(src)], timeout=180)
    p = _project("rr")
    data, _ = call("pcb_setup", project=p, outline_file=str(d / "rr.step"), outline_keep_position=True, render=False)
    assert data["outline_import"]["shapes"] == {"lines": 4, "arcs": 4, "circles": 0}, data["outline_import"]
    xor, area, dev = fx.compare(src, _pcb(p))
    assert xor <= 0.001 and dev <= 0.001, (xor, dev)
    txt = _pcb(p).read_text(encoding="utf-8")
    assert txt.count("(gr_arc") == 4 and txt.count("(gr_line") == 4, (txt.count("(gr_arc"), txt.count("(gr_line"))
    assert '(property "COPPER_OUTLINE" "src=file' in txt


# ── A10-A13, A15, A22: tool behaviour ─────────────────────────────────────
def test_dry_run_writes_nothing_and_works_on_an_open_board():
    f = _fancy()
    p = _project("dry")
    pcb = _pcb(p)
    before = pcb.read_bytes()
    lock = pcb.parent / f"~{pcb.name}.lck"
    lock.write_text('{"hostname":"x","username":"y"}', encoding="utf-8")       # the board is "open in KiCad"
    try:
        data, r = call("pcb_setup", project=p, outline_file=str(f["step"]), outline_dry_run=True)
        assert data["dry_run"] and data["outline_import"]["candidates"] and len(_images(r)) == 1
        assert data["outline_import"]["size"] == [60.0, round(data["outline_import"]["size"][1], 3)]
        e = call_err("pcb_setup", project=p, outline_file=str(f["step"]))       # the real write is still refused
        assert "OPEN" in e["error"]["code"], e["error"]
    finally:
        lock.unlink()
    assert pcb.read_bytes() == before


def test_open_outline_bad_arguments_and_missing_file():
    f = _fancy()
    p = _project("errs")
    pcb = _pcb(p)
    before = pcb.read_bytes()
    lines = f["dxf"].read_text(encoding="utf-8", errors="replace").splitlines()
    # drop the first LINE entity that belongs to the outer contour
    k = next(i for i in range(0, len(lines) - 1, 2) if lines[i].strip() == "0" and lines[i + 1].strip() == "LINE")
    j = next(i for i in range(k + 2, len(lines) - 1, 2) if lines[i].strip() == "0")
    broken = _TMP / "open.dxf"
    broken.write_text("\n".join(lines[:k] + lines[j:]) + "\n", encoding="utf-8")
    try:
        cadout.load(broken)
        opened = False
    except cadout.OutlineError as e:
        opened = e.code == "OUTLINE_OPEN" and len(e.data["gaps"]) == 2
    if opened:
        e = call_err("pcb_setup", project=p, outline_file=str(broken))["error"]
        assert e["code"] == "OUTLINE_OPEN" and "open ends" in e["message"], e
    else:   # the removed line was part of a cutout: the import still works, the open contour is reported
        ol, rep = cadout.load(broken)
        assert ol["ignored"]["open_chains"] >= 1
    e = call_err("pcb_setup", project=p, outline_file=str(f["step"]), width=40, height=30)["error"]
    assert e["code"] == "BAD_ARGUMENT"
    e = call_err("pcb_setup", project=p, outline_file=str(f["step"]), auto_size=True)["error"]
    assert e["code"] == "BAD_ARGUMENT"
    e = call_err("pcb_setup", project=p, outline_file="nope.step")["error"]
    assert e["code"] == "OUTLINE_FILE_NOT_FOUND"
    e = call_err("pcb_setup", project=p, outline_file="auto")["error"]
    assert e["code"] == "OUTLINE_FILE_NOT_FOUND" and "mechanical" in e["message"]
    e = call_err("pcb_setup", project=p, outline_file=str(f["step"]), outline_pick=99)["error"]
    assert e["code"] == "BAD_ARGUMENT"
    e = call_err("pcb_setup", project=p, outline_dry_run=True)["error"]
    assert e["code"] == "BAD_ARGUMENT"
    assert pcb.read_bytes() == before, "a refused import must not write"
    # 'auto' takes the single file in <project>/mechanical/
    mech = Path(p) / "mechanical"
    mech.mkdir()
    shutil.copy2(f["step"], mech / "Board Body.STEP")
    data, _ = call("pcb_setup", project=p, outline_file="auto", render=False)
    assert data["outline_import"]["file"] == "Board Body.STEP"
    shutil.copy2(f["dxf"], mech / "second.dxf")
    e = call_err("pcb_setup", project=p, outline_file="auto")["error"]
    assert e["code"] == "OUTLINE_AMBIGUOUS" and "second.dxf" in e["message"]


def test_reimport_is_idempotent_and_holes_follow_the_arguments():
    f = _fancy()
    p = _project("idem")
    d1, _ = call("pcb_setup", project=p, outline_file=str(f["step"]), render=False)
    s1 = board.read(projmod.resolve(p), what=["summary", "footprints", "drawings"])
    d2, _ = call("pcb_setup", project=p, outline_file=str(f["step"]), render=False)
    s2 = board.read(projmod.resolve(p), what=["summary", "footprints", "drawings"])
    edge = lambda s: sum(1 for x in s["drawings"] if x["layer"] == "Edge.Cuts")
    holes = lambda s: sorted((x["x"], x["y"]) for x in s["footprints"] if "MountingHole" in x["footprint"])
    assert edge(s1) == edge(s2) == sum(d1["outline_import"]["shapes"].values()) and len(holes(s1)) == 4 and holes(s1) == holes(s2)
    assert d1["outline"]["bbox"][:2] == [100.0, 100.0], d1["outline"]            # moved to the default origin
    d3, _ = call("pcb_setup", project=p, outline_file=str(f["step"]), holes="none", render=False)
    assert d3["outline_import"]["holes"] == [] and not holes(board.read(projmod.resolve(p), what=["footprints"]))
    d4, _ = call("pcb_setup", project=p, outline_file=str(f["step"]), holes="M2.5", origin=[50, 60], render=False)
    s4 = board.read(projmod.resolve(p), what=["footprints"])
    assert all("M2.5" in x["footprint"] for x in s4["footprints"] if "MountingHole" in x["footprint"]) and len(holes(s4)) == 4
    assert d4["outline"]["bbox"][:2] == [50.0, 60.0]
    d5, _ = call("pcb_setup", project=p, outline_file=str(f["step"]), outline_holes="all", outline_flip=True, render=False)
    assert d5["outline_import"]["cutouts"] == 2 and len(d5["outline_import"]["holes"]) >= 4


def test_enclosure_through_the_tool_sets_holes_clearance_and_height_note():
    d = _TMP / "enc tool"
    d.mkdir(exist_ok=True)
    fx.enclosure(d / "case.step", flags="walls")
    p = _project("enc")
    data, r = call("pcb_setup", project=p, outline_file=str(d / "case.step"))
    oi = data["outline_import"]
    assert oi["mode"] == "enclosure" and oi["size"] == [75.0, 45.0] and oi["clearance"] == 0.5, oi
    assert oi["shapes"] == {"lines": 8, "arcs": 8, "circles": 0}, oi["shapes"]        # outer r=2.5 corners + grown rib
    assert {"OUTLINE_AMBIGUOUS", "HEIGHT_LIMIT"} <= set(_codes(data)) and oi["candidates"], _codes(data)
    assert len(_images(r)) == 2, "an uncertain pick returns the pick picture and the board render"
    txt = _pcb(p).read_text(encoding="utf-8")
    assert "mode=enclosure" in txt and "top=12.4" in txt and "bottom=4.0" in txt
    sz = {k: len(json.dumps(v)) for k, v in data.items()}
    assert sz["outline_import"] <= 1300 and sum(v for k, v in sz.items() if k != "rules") <= 3600, sz
    ol = board.read(projmod.resolve(p), what=["summary"])["outline"]
    assert ol["src"] == "file" and len(ol["holes"]) == 1 and abs(ol["area"] - (75 * 45 - (4 - math.pi) * 2.5 ** 2 - (5 * 11 - (4 - math.pi) * 0.25))) < 0.6, ol["area"]
    # the caller confirming the mode makes it a quiet, confident import
    data, r = call("pcb_setup", project=p, outline_file=str(d / "case.step"), outline_mode="enclosure", outline_clearance=1.0)
    assert "OUTLINE_AMBIGUOUS" not in _codes(data) and data["outline_import"]["size"] == [74.0, 44.0] and len(_images(r)) == 1


def test_outline_on_a_routed_board_keeps_copper_and_warns():
    import test_pcb as T
    d = _TMP / "routed"
    if d.exists():
        shutil.rmtree(d)
    T.build(d)
    p = str(d)
    call("pcb_sync", project=p, render=False)
    call("pcb_setup", project=p, width=60, height=45, render=False)
    call("pcb_place", project=p, auto=True, render=False)
    call("pcb_route", project=p, tracks=[{"from": "R1.2", "to": "D1.2"}], render=False)
    pr = projmod.resolve(p)
    s0 = board.read(pr, what=["footprints", "tracks", "nets"])
    assert s0["tracks"] >= 1
    src = _TMP / "small.kicad_pcb"
    fx.rounded_board(src, w=30, h=20, r=2)
    dxf = _TMP / "small.dxf"
    env.run(["pcb", "export", "dxf", "--layers", "Edge.Cuts", "--output-units", "mm", "--mode-single", "-o", str(dxf), str(src)], timeout=120)
    data, _ = call("pcb_setup", project=p, outline_file=str(dxf), render=False)
    f = next(x for x in data["findings"] if x["code"] == "OUTLINE_REPLACES_ROUTED")
    assert "outside the new outline" in f["message"] and "checkpoint(action='restore'" in f["fix"], f
    s1 = board.read(pr, what=["footprints", "tracks", "nets"])
    key = lambda s: sorted((t["uuid"], str(t.get("start")), str(t.get("end")), str(t.get("at")), t.get("layer")) for t in s["track_list"])
    assert key(s0) == key(s1), "tracks and vias must be untouched"
    pos = lambda s: sorted((x["ref"], x["x"], x["y"], x["rot"]) for x in s["footprints"] if "board_only" not in x.get("flags", []))
    assert pos(s0) == pos(s1) and s0["nets"] == s1["nets"]
    assert data["outline"]["width"] == 30.0 and data["outline"]["height"] == 20.0
    e = call_err("pcb_setup", project=p, auto_size=True)["error"]
    assert e["code"] == "BAD_ARGUMENT" and "tracks" in e["message"]
    assert key(board.read(pr, what=["tracks"])) == key(s0)


# ── A7 / A24: a real fused module model with a castellated edge ───────────
def _pico():
    kp = env.kicad_python()
    root = Path(kp).parent.parent / "share" / "kicad" / "3dmodels" if kp else None
    p = root / "Module.3dshapes" / "RaspberryPi_Pico.step" if root else None
    return p if p and p.is_file() else None


def test_pico_model_and_placement_inside_a_castellated_outline():
    src = _pico()
    if not src:
        print("  (skipped: RaspberryPi_Pico.step is not installed)")
        return
    ol, rep = cadout.load(src, keep_position=True)
    assert rep["mode"] == "board" and abs(ol["size"][0] - 21.0) <= 0.01 and abs(ol["size"][1] - 51.0) <= 0.01 and ol["thickness"] == 1.0, (ol["size"], ol["thickness"])
    cx, cy = (ol["bbox"][0] + ol["bbox"][2]) / 2, (ol["bbox"][1] + ol["bbox"][3]) / 2
    hs = sorted((round(h["x"] - cx, 2), round(h["y"] - cy, 2), h["d"]) for h in ol["holes"])
    assert len(hs) == 4 and all(abs(abs(x) - 5.7) <= 0.01 and abs(abs(y) - 23.5) <= 0.01 and abs(d - 2.1) <= 0.01 for x, y, d in hs), hs
    assert all(h["size"] == "M2" for h in ol["holes"]) and ol["ignored"]["drills"] > 20
    p = _project("pico")
    data, _ = call("pcb_setup", project=p, outline_file=str(src), render=False)
    o = board.read(projmod.resolve(p), what=["summary"])["outline"]
    assert o["closed"] and len(o["polygon"]) <= 400 and o.get("polygon_tol", 0) <= 0.2, (len(o["polygon"]), o.get("polygon_tol"))
    # a bigger outline of the same family: the fancy board, with its cutout and arcs; nothing may sit on them
    f = _fancy()
    q = _project("fancy place")
    call("pcb_setup", project=q, outline_file=str(f["step"]), outline_keep_position=True, render=False)
    data, _ = call("pcb_place", project=q, auto=True, render=False)
    assert not [x for x in data["findings"] if x["code"] == "DID_NOT_FIT"], data["findings"]
    assert data["verify"]["courtyard_overlaps"] == 0
    snap = board.read(projmod.resolve(q), what=["footprints"])
    from copper_kicad.pcb.elegant import Bitmap
    bm = Bitmap(snap["outline"]["polygon"], snap["outline"].get("holes") or [], snap["outline"]["bbox"], 0.0)
    for fp in snap["footprints"]:
        if "board_only" in fp.get("flags", []):
            continue
        b = fp["bbox"]
        assert bm.free((b[0] + 0.3, b[1] + 0.3, b[2] - 0.3, b[3] - 0.3)), f"{fp['ref']} sits on a cutout or outside the curved edge: {b}"
        assert not (125 < fp["x"] < 135 and 117 < fp["y"] < 123), f"{fp['ref']} is inside the cutout"


# ── A14 / A19: corpus (slow) ──────────────────────────────────────────────
def test_corpus_of_library_models_never_crashes():
    if not os.environ.get("COPPER_TEST_SLOW"):
        print("  (skipped: set COPPER_TEST_SLOW=1)")
        return
    kp = env.kicad_python()
    root = Path(kp).parent.parent / "share" / "kicad" / "3dmodels"
    files = sorted(root.rglob("*.step"))
    if len(files) < 200:
        print("  (skipped: 3D library not installed)")
        return
    rnd = random.Random(7)
    sample = rnd.sample(files, 150)
    t0, mb, fails, confident_board, no_cand = time.time(), 0.0, [], [], 0
    for p in sample:
        mb += p.stat().st_size / 1048576
        try:
            ol, rep = cadout.load(p)
            if rep["mode"] == "board" and not rep["ambiguous"]:
                confident_board.append(p.name)
            if rep["ambiguous"] and not rep.get("candidates"):
                no_cand += 1
        except cadout.OutlineError as e:
            if e.code == "OUTLINE_PARSE_FAILED":
                fails.append((p.name, e.message[:120]))
        except Exception as e:  # noqa: BLE001
            fails.append((p.name, repr(e)[:120]))
    dt = time.time() - t0
    print(f"  {len(sample)} files, {mb:.0f} MB in {dt:.0f} s = {mb / dt:.1f} MB/s; confident boards: {confident_board}")
    assert not fails, fails[:5]
    assert mb / dt >= 2.0, f"{mb / dt:.1f} MB/s"
    real_boards = [n for n in confident_board if not any(k in n for k in ("Module", "DCDC", "Converter", "Pico", "ESP", "Arduino", "WROOM", "Board"))]
    assert len(real_boards) <= 2, real_boards
    assert no_cand == 0


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
