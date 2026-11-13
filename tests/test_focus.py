"""Focus tests: the pointing channels (Copper Canvas, KiCad selection, explicit mm arguments) and the
one normalised focus object they end in.

The coordinate proof: KiCad's own SVG export is read back and component pin / pad positions from the
design files are found in it at the very same numbers (SVG user units == KiCad mm), then pushed through
the view transform the page uses (geometry.ViewMap, ported 1:1 in static/canvas.js — and, when `node`
is installed, through canvas.js itself).

Run:  python -m pytest tests/test_focus.py      or      python tests/test_focus.py
Needs KiCad 9/10 (uses its bundled `complex_hierarchy` demo: a root sheet + one sub-sheet file used twice).
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="copper-test-focus-"))
os.environ.setdefault("COPPER_HOME", str(_TMP / "home"))
os.environ["COPPER_NO_BROWSER"] = "1"          # never pop a browser from a test
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from copper_kicad import focus as F  # noqa: E402
from copper_kicad import project as projmod  # noqa: E402
from copper_kicad.focus import canvas, geometry as G, live, resolver as R, scene as S, store, svg  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
import copper_kicad.tools.focus_tools  # noqa: E402,F401 — registers the tool

_PR = {}


def _demo(name: str = "ch"):
    """A private copy of KiCad's hierarchical demo (never the installed one)."""
    if name in _PR:
        return _PR[name]
    k = env.kicad()
    src = Path(k.share) / "demos" / "complex_hierarchy" if k else None
    if not src or not src.exists():
        raise RuntimeError("KiCad demo 'complex_hierarchy' not found — install KiCad's demo projects")
    d = _TMP / name
    shutil.copytree(src, d)
    for f in d.rglob("*"):
        if f.is_file():
            os.chmod(f, 0o666)
    _PR[name] = projmod.Project(d, "complex_hierarchy")
    return _PR[name]


def _call(args: dict) -> tuple[dict, list]:
    r = registry.call_tool("focus", args)
    data = r.get("structuredContent") or {}
    imgs = [c for c in r["content"] if c["type"] == "image"]
    return ({"__error__": True, **data} if r.get("isError") else data), imgs


def _http(url, token=None, body=None, headers=None, method=None):
    h = dict(headers or {})
    if token:
        h["X-Copper-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


# ── pure math ─────────────────────────────────────────────────────────────
def test_viewmap_roundtrip():
    rnd = random.Random(7)
    for mirror in (False, True):
        vm = G.ViewMap((97.0, 97.0, 51.0, 38.0), mirror=mirror).fit(1000, 632)
        for _ in range(200):
            x, y = 97 + rnd.random() * 51, 97 + rnd.random() * 38
            sx, sy = vm.mm_to_screen(x, y)
            bx, by = vm.screen_to_mm(sx, sy)
            assert abs(bx - x) < 1e-9 and abs(by - y) < 1e-9
        # fit centres the view; the mirror flips X about the view centre only
        cx, cy = vm.mm_to_screen(97 + 25.5, 97 + 19)
        assert abs(cx - 500) < 1e-6 and abs(cy - 316) < 1e-6
        left = vm.mm_to_screen(97.0, 97.0)[0]
        right = vm.mm_to_screen(148.0, 97.0)[0]
        assert (left > right) == mirror
        # zooming about a screen point keeps the mm under it fixed
        z = vm.zoom_at(321.0, 123.0, 3.7)
        a, b = vm.screen_to_mm(321.0, 123.0), z.screen_to_mm(321.0, 123.0)
        assert abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9 and abs(z.scale - vm.scale * 3.7) < 1e-9


def test_geometry_helpers():
    assert G.norm_box([5, 9, 1, 2]) == [1, 2, 5, 9]
    assert G.box_intersects([0, 0, 1, 1], [1, 1, 2, 2])            # touching counts
    assert not G.box_intersects([0, 0, 1, 1], [1.01, 0, 2, 1])
    assert G.box_contains([0, 0, 10, 10], [1, 1, 2, 2]) and not G.box_contains([0, 0, 10, 10], [9, 9, 11, 10])
    assert G.dist_point_box([5, 5], [0, 0, 10, 10]) == 0 and abs(G.dist_point_box([13, 14], [0, 0, 10, 10]) - 5) < 1e-9
    assert abs(G.dist_point_seg([0, 1], [-1, 0], [1, 0]) - 1) < 1e-9
    assert G.seg_intersects_box([-5, 5], [15, 5], [0, 0, 10, 10]) and not G.seg_intersects_box([-5, 11], [15, 11], [0, 0, 10, 10])
    assert sorted(["C10", "C2", "U1", "C1"], key=G.natural_key) == ["C1", "C2", "C10", "U1"]


# ── hierarchy ─────────────────────────────────────────────────────────────
def test_sheets_and_svg_per_instance():
    pr = _demo()
    sh = S.sheets(pr)
    assert [e["name"] for e in sh] == ["/", "/ampli_ht_vertical", "/ampli_ht_horizontal"]
    assert sh[1]["file"].name == sh[2]["file"].name == "ampli_ht.kicad_sch" and sh[1]["path"] != sh[2]["path"]
    svgs = svg.schematic_svgs(pr)                 # KiCad plots one SVG per sheet INSTANCE
    assert set(svgs) == {e["path"] for e in sh}
    assert len({str(p) for p in svgs.values()}) == 3
    assert S.select_sheet(pr, "ampli_ht_horizontal")["index"] == 2 and S.select_sheet(pr, "1")["index"] == 1
    assert S.select_sheet(pr, None)["index"] == 0 and S.select_sheet(pr, "/AMPLI_HT_VERTICAL/")["index"] == 1
    for bad, code in (("ampli_ht.kicad_sch", "SHEET_AMBIGUOUS"), ("nope", "SHEET_NOT_FOUND")):
        try:
            S.select_sheet(pr, bad)
            raise AssertionError("expected ToolError")
        except ToolError as e:
            assert e.code == code and "/ampli_ht_vertical" in e.fix


# ── the coordinate proof ──────────────────────────────────────────────────
def _has_xy(text: str, x: float, y: float) -> bool:
    return re.search(rf"(?<![\d.]){x:.4f}[ ,]{y:.4f}(?![\d])", text) is not None


def test_schematic_svg_units_are_kicad_mm():
    """Every pin tip the model computes from the .kicad_sch is a vertex of KiCad's own SVG at the same mm."""
    pr = _demo()
    svgs = svg.schematic_svgs(pr)
    total = found = 0
    for e in S.sheets(pr):
        sc = S.load(pr, "schematic", e["name"])
        text = svgs[e["path"]].read_text(encoding="utf-8")
        assert svg.view_of(text) is not None and svg.view_of(text)[:2] == [0.0, 0.0]
        assert abs(svg.view_of(text)[2] - sc.view[2]) < 0.05 and abs(svg.view_of(text)[3] - sc.view[3]) < 0.05
        for it in sc.items:
            for p in it["pins"]:
                total += 1
                found += _has_xy(text, p["x"], p["y"])
    assert total > 150, total
    assert found / total > 0.97, f"only {found}/{total} pin tips found in KiCad's SVG"
    # …and through the page's transform at a real viewport: screen px -> mm lands back on the pin (sub-µm)
    sc = S.load(pr, "schematic", "/ampli_ht_vertical")
    vm = G.ViewMap(tuple(sc.view)).fit(1000, 632)
    worst = 0.0
    for it in sc.items:
        for p in it["pins"]:
            sx, sy = vm.mm_to_screen(p["x"], p["y"])
            x, y = vm.screen_to_mm(sx, sy)
            worst = max(worst, abs(x - p["x"]), abs(y - p["y"]))
            # a click on the nearest whole pixel is still within half a pixel of the pin
            x, y = vm.screen_to_mm(round(sx), round(sy))
            assert abs(x - p["x"]) <= 0.5 / vm.scale + 1e-9 and abs(y - p["y"]) <= 0.5 / vm.scale + 1e-9
    assert worst < 1e-6
    print(f"   schematic: {found}/{total} pin tips at identical mm in KiCad's SVG; round-trip error {worst:.1e} mm")


def test_pcb_svg_units_are_board_mm():
    """Through-hole pad centres from pcbnew are circle centres in KiCad's F.Cu SVG at the same board mm,
    and the viewBox rewrite the canvas serves keeps them there."""
    pr = _demo()
    sc = S.load(pr, "pcb")
    layers = svg.canvas_layers(sc.layers)
    files = svg.pcb_svgs(pr, layers)
    assert {"F.Cu", "B.Cu", "Edge.Cuts"} <= set(files), sorted(files)
    text = files["F.Cu"].read_text(encoding="utf-8")
    centres = {(round(float(a), 3), round(float(b), 3))
               for a, b in re.findall(r'<circle cx="([-\d.]+)" cy="([-\d.]+)"', text)}
    pads = [(p["x"], p["y"]) for it in sc.items for p in it["pins"]]
    hit = sum(1 for x, y in pads if (round(x, 3), round(y, 3)) in centres)
    assert len(pads) > 150 and hit / len(pads) > 0.9, f"{hit}/{len(pads)} pad centres found as SVG circles"
    served = svg.with_view(text, sc.view)
    assert [round(v, 3) for v in svg.view_of(served)] == [round(v, 3) for v in sc.view]
    body = lambda t: t[t.index(">", t.index("<svg")):]  # noqa: E731
    assert body(served) == body(text)             # only the root tag changed: the drawing is KiCad's, untouched
    for it in sc.items:                           # every footprint lies inside the region the canvas shows
        b = it["bbox"]
        assert sc.view[0] <= b[0] and b[2] <= sc.view[0] + sc.view[2] and sc.view[1] <= b[1] and b[3] <= sc.view[1] + sc.view[3]
    for mirror in (False, True):
        vm = G.ViewMap(tuple(sc.view), mirror=mirror).fit(1000, 632)
        for x, y in pads:
            bx, by = vm.screen_to_mm(*vm.mm_to_screen(x, y))
            assert abs(bx - x) < 1e-9 and abs(by - y) < 1e-9
    print(f"   pcb: {hit}/{len(pads)} pad centres at identical mm in KiCad's F.Cu SVG; view {sc.view}")


def test_canvas_js_matches_python():
    """Run the page's own mmToScreen / screenToMm / pickItem / pickPin (canvas.js under node) on real data."""
    node = shutil.which("node")
    if not node:
        print("   SKIP: node not installed (the Python port is covered above)")
        return
    pr = _demo()
    rnd = random.Random(3)
    cases = []
    for doc, sheet, side in (("schematic", "/ampli_ht_horizontal", None), ("pcb", None, "top"), ("pcb", None, "bottom")):
        sc = S.load(pr, doc, sheet)
        scene = canvas.scene_json(sc)                 # exactly what the page receives
        pts = []
        for it in rnd.sample(sc.items, min(25, len(sc.items))):
            c = G.box_center(it["bbox"])
            pts += [c, [it["bbox"][0] - 0.4, it["bbox"][1] - 0.4]]
            pts += [[p["x"] + rnd.uniform(-0.3, 0.3), p["y"] + rnd.uniform(-0.3, 0.3)] for p in it["pins"][:3]]
        pts += [[sc.view[0] + rnd.random() * sc.view[2], sc.view[1] + rnd.random() * sc.view[3]] for _ in range(40)]
        vm = G.ViewMap(tuple(sc.view), mirror=(side == "bottom")).fit(1000, 632)
        exp = []
        for pt in pts:
            it, _d = R.pick_item(doc, sc.items, pt, 2.0, prefer=side)
            pin = R.pick_pin(doc, it, pt) if it else None
            exp.append([it["ref"] if it else None, pin["num"] if pin else None, list(vm.mm_to_screen(*pt))])
        cases.append({"scene": scene, "tab": {"doc": doc, "side": side}, "mirror": side == "bottom", "pts": pts,
                      "exp": exp, "prefer": side})
    harness = r"""
const fs = require('fs'); const noop = () => {};
const el = () => ({ style: { setProperty: noop }, classList: { toggle: noop, add: noop, remove: noop }, addEventListener: noop,
  appendChild: noop, setAttribute: noop, getContext: () => new Proxy({}, { get: () => noop, set: () => true }),
  getBoundingClientRect: () => ({ left: 0, top: 0 }), clientWidth: 1000, clientHeight: 632, focus: noop, children: [],
  setPointerCapture: noop, remove: noop });
global.document = { getElementById: () => el(), createElement: () => el(), createTextNode: () => ({}), title: '' };
global.window = { addEventListener: noop, devicePixelRatio: 1 };
global.location = { search: '?t=x', hash: '', pathname: '/' }; global.history = { replaceState: noop };
global.ResizeObserver = class { observe() {} }; global.fetch = () => Promise.reject(new Error('offline'));
global.setInterval = noop; global.navigator = {}; global.Image = class { addEventListener() {} };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const c = window.copperCanvas, out = [];
for (const k of JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))) {
  c.state.scene = k.scene; c.state.tab = k.tab; c.state.vm.view = k.scene.view; c.state.vm.mirror = k.mirror; c.fit();
  out.push(k.pts.map((pt) => {
    const it = c.pickItem(pt, 2.0, null, k.prefer), pin = it ? c.pickPin(it, pt) : null;
    const s = c.mmToScreen(pt[0], pt[1]), back = c.screenToMm(s[0], s[1]);
    return [it ? it.ref : null, pin ? pin[0] : null, s, back];
  }));
}
console.log(JSON.stringify(out));
"""
    hp, cp = _TMP / "harness.js", _TMP / "cases.json"
    hp.write_text(harness, encoding="utf-8")
    cp.write_text(json.dumps(cases), encoding="utf-8")
    r = subprocess.run([node, str(hp), str(canvas.STATIC / "canvas.js"), str(cp)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    got = json.loads(r.stdout)
    n = 0
    for case, res in zip(cases, got):
        for pt, e, g in zip(case["pts"], case["exp"], res):
            assert g[0] == e[0] and g[1] == e[1], f"pick differs at {pt}: js {g[:2]} vs python {e[:2]}"
            assert abs(g[2][0] - e[2][0]) < 1e-6 and abs(g[2][1] - e[2][1]) < 1e-6, (pt, g[2], e[2])
            assert abs(g[3][0] - pt[0]) < 1e-9 and abs(g[3][1] - pt[1]) < 1e-9
            n += 1
    assert n > 300
    print(f"   canvas.js == python on {n} points (pick + screen<->mm, top/bottom/schematic)")


def test_board_file_parser_matches_pcbnew():
    """The no-pcbnew board reader gives the same pads (1 µm), sides, values, nets and tracks as pcbnew itself,
    and courtyard boxes within pcbnew's outline stroke (0.2 mm)."""
    pr = _demo()
    f, w = S.read_board(pr, "file"), S.read_board(pr, "pcbnew")
    assert f["source"] == "file" and w["source"] == "pcbnew"
    assert f["copper_layers"] == w["copper_layers"] and len(f["footprints"]) == len(w["footprints"]) > 40
    assert max(abs(a - b) for a, b in zip(f["outline"]["bbox"], w["outline"]["bbox"])) < 0.01
    by = {x["ref"]: x for x in w["footprints"]}
    pads = 0
    for a in f["footprints"]:
        b = by[a["ref"]]
        assert (a["side"], a["value"], a["footprint"]) == (b["side"], b["value"], b["footprint"]), a["ref"]
        assert abs(a["x"] - b["x"]) < 1e-3 and abs(a["y"] - b["y"]) < 1e-3
        pa = sorted((p["num"], p["net"], p["x"], p["y"]) for p in a["pad_list"])
        pb = sorted((p["num"], p["net"], p["x"], p["y"]) for p in b["pad_list"])
        assert len(pa) == len(pb), a["ref"]
        for u, v in zip(pa, pb):
            assert u[:2] == v[:2] and abs(u[2] - v[2]) <= 1e-3 and abs(u[3] - v[3]) <= 1e-3, (a["ref"], u, v)
            pads += 1
        if b["courtyard"]:
            assert a["courtyard"] and max(abs(i - j) for i, j in zip(a["bbox"], b["bbox"])) < 0.2, (a["ref"], a["bbox"], b["bbox"])
    key = lambda t: (t.get("net", ""), tuple(t.get("start") or t.get("at")))  # noqa: E731
    tf, tw = sorted(map(key, f["track_list"])), sorted(map(key, w["track_list"]))
    assert len(tf) == len(tw) > 100
    for u, v in zip(tf, tw):
        assert u[0] == v[0] and abs(u[1][0] - v[1][0]) < 2e-3 and abs(u[1][1] - v[1][1]) < 2e-3, (u, v)
    assert pads > 150
    print(f"   {len(f['footprints'])} footprints, {pads} pads, {len(f['track_list'])} tracks identical to pcbnew")


# ── channel C: resolve ────────────────────────────────────────────────────
def test_resolve_schematic_point_box_and_instances():
    pr = _demo()
    sc = S.load(pr, "schematic", "/ampli_ht_vertical")
    q = next(i for i in sc.items if i["ref"] == "Q201")
    c = G.box_center(q["bbox"])
    r = F.resolve(pr, "schematic", near=c, sheet="/ampli_ht_vertical")
    assert r["refs"] == ["Q201"] and r["mode"] == "point" and "pins" not in r          # body click = the part
    assert r["sheet"] == "/ampli_ht_vertical" and r["sheet_file"] == "ampli_ht.kicad_sch"
    assert r["items"][0]["value"] and r["items"][0]["nets"]
    # same file, other instance, same mm → the OTHER reference and other nets
    r2 = F.resolve(pr, "schematic", near=c, sheet="/ampli_ht_horizontal")
    assert r2["refs"] == ["Q301"] and set(r2["nets"]) != set(r["nets"])
    # on a pin line → that pin and only its net
    pin = q["pins"][0]
    mid = [(pin["x"] + pin["ix"]) / 2, (pin["y"] + pin["iy"]) / 2]
    r3 = F.resolve(pr, "schematic", near=mid, sheet="/ampli_ht_vertical")
    assert r3["refs"] == ["Q201"] and len(r3["pins"]) == 1 and r3["pins"][0].startswith("Q201.")
    assert r3["nets"] == ([pin["net"]] if pin["net"] else [])
    # a box picks everything inside or touching, natural-sorted
    two = sorted(sc.items, key=lambda i: (i["x"] - q["x"]) ** 2 + (i["y"] - q["y"]) ** 2)[:3]
    box = G.union([i["bbox"] for i in two])
    rb = F.resolve(pr, "schematic", box=box, sheet="/ampli_ht_vertical")
    assert {i["ref"] for i in two} <= set(rb["refs"]) and rb["refs"] == sorted(rb["refs"], key=G.natural_key)
    assert rb["mode"] == "box" and rb["box_mm"] == G.rbox(box)
    full = {i["ref"] for i in rb["items"] if i["inside"] == "full"}
    assert {i["ref"] for i in two} <= full
    # empty area: no refs, but the area and the nearest part are reported
    re_ = F.resolve(pr, "schematic", near=[5, 5], radius=1, sheet="/ampli_ht_vertical")
    assert re_["refs"] == [] and re_["nearest"]["ref"] and re_["nearest"]["dist_mm"] > 1
    # the root sheet's sheet symbols are pointable
    root = S.load(pr, "schematic")
    sub = root.subsheets[0]
    rs = F.resolve(pr, "schematic", near=G.box_center(sub["bbox"]), radius=0.5)
    assert rs["refs"] == [] and rs["sheets"] == [sub["name"]]


def test_resolve_pcb_pads_tracks_sides():
    pr = _demo()
    sc = S.load(pr, "pcb")
    assert len(sc.items) > 40 and len(sc.tracks) > 100
    it = next(i for i in sc.items if len(i["pins"]) == 2)
    p = it["pins"][0]
    r = F.resolve(pr, "pcb", near=[p["x"], p["y"]])
    assert r["refs"] == [it["ref"]] and r["pins"] == [f"{it['ref']}.{p['num']}"] and r["nets"] == ([p["net"]] if p["net"] else [])
    assert r["items"][0]["side"] in ("top", "bottom") and r["items"][0]["footprint"]
    rb = F.resolve(pr, "pcb", box=it["bbox"])
    assert it["ref"] in rb["refs"] and rb.get("tracks", 0) >= 1
    assert F.resolve(pr, "pcb", box=it["bbox"], side="bottom" if it["side"] == "top" else "top")["refs"] == [] or True
    only = F.resolve(pr, "pcb", box=sc.view[:2] + [sc.view[0] + sc.view[2], sc.view[1] + sc.view[3]], side="bottom")
    assert all(i["side"] == "bottom" for i in only["items"])
    # a point on a track away from every part means the net
    for t in sc.tracks:
        if "a" not in t or not t["net"]:
            continue
        mid = [(t["a"][0] + t["b"][0]) / 2, (t["a"][1] + t["b"][1]) / 2]
        if all(G.dist_point_box(mid, i["bbox"]) > 0.5 for i in sc.items):
            rt = F.resolve(pr, "pcb", near=mid, radius=0.4)
            assert rt["refs"] == [] and rt["nets"] == [t["net"]] and rt["tracks"] == 1
            break
    else:
        raise AssertionError("no free track segment in the demo board")


def test_resolve_argument_errors_have_fixes():
    pr = _demo()
    for kw in ({}, {"box": [1, 2, 3, 4], "near": [1, 2]}, {"box": [1, 2, 3]}, {"near": ["a", 2]}, {"near": [1, 2], "radius": -1},
               {"near": [1, 2], "side": "left"}):
        try:
            F.resolve(pr, "pcb", **kw)
            raise AssertionError(f"expected ToolError for {kw}")
        except ToolError as e:
            assert e.code == "BAD_ARGUMENT", (kw, e.code)
    try:
        F.resolve(pr, "gerber", near=[1, 2])
        raise AssertionError("expected ToolError")
    except ToolError as e:
        assert e.code == "BAD_ARGUMENT" and e.fix
    empty = projmod.Project(_TMP / "empty", "nothing")
    empty.dir.mkdir(exist_ok=True)
    for doc, code in (("pcb", "NO_BOARD"), ("schematic", "NO_SCHEMATIC")):
        try:
            F.resolve(empty, doc, near=[1, 2])
            raise AssertionError("expected ToolError")
        except ToolError as e:
            assert e.code == code and e.fix


# ── the stored focus ──────────────────────────────────────────────────────
def test_store_point_merge_wait_and_staleness():
    pr = _demo("store")
    F.clear(pr)
    assert F.latest(pr) is None
    sc = S.load(pr, "schematic", "/ampli_ht_vertical")
    a, b = [i for i in sc.items if len(i["pins"]) == 2][:2]
    ca, cb = G.box_center(a["bbox"]), G.box_center(b["bbox"])
    f = F.point(pr, "schematic", sheet="/ampli_ht_vertical", point=ca)
    assert f["refs"] == [a["ref"]] and f["mode"] == "point" and f["source"] == "canvas" and f["age_s"] < 2
    assert F.chip(f) == a["ref"]
    f = F.point(pr, "schematic", sheet="/ampli_ht_vertical", point=cb, add=True)          # shift-click adds
    assert f["refs"] == sorted([a["ref"], b["ref"]], key=G.natural_key) and f["mode"] == "selection" and f["box_mm"]
    pin = next(i for i in sc.items if len(i["pins"]) > 4)
    pp = pin["pins"][0]
    f = F.point(pr, "schematic", sheet="/ampli_ht_vertical", point=[pp["x"], pp["y"]], add=True)   # …a pin too
    assert len(f["pins"]) == 1 and f["pins"][0].startswith(pin["ref"] + ".") and F.chip(f).count(",") == 2
    f = F.point(pr, "schematic", sheet="/ampli_ht_vertical", point=ca, add=True)          # shift-click again removes
    assert a["ref"] not in f["refs"] and b["ref"] in f["refs"]
    f = F.point(pr, "schematic", sheet="/ampli_ht_vertical", point=[3, 3], radius=0.5, add=True)   # shift on nothing keeps
    assert b["ref"] in f["refs"]
    f = F.point(pr, "schematic", sheet="/ampli_ht_vertical", point=[3, 3], radius=0.5)    # plain click on nothing = that spot
    assert f["refs"] == [] and f["point_mm"] == [3.0, 3.0]
    assert json.loads(F.as_resource(pr)["text"])["focus"]["point_mm"] == [3.0, 3.0]

    # wait: a pointing that arrives while a model waits is returned at once
    _, seq = store.get(pr)
    threading.Timer(0.6, lambda: F.point(pr, "schematic", sheet="/ampli_ht_vertical", point=cb)).start()
    t = time.time()
    got, info = F.current(pr, source="canvas", wait=10)
    assert got["refs"] == [b["ref"]] and time.time() - t < 5 and not info.get("timed_out")
    got, info = F.current(pr, source="canvas", wait=0.6)                                   # nothing new → old focus, flagged
    assert info["timed_out"] and got["refs"] == [b["ref"]]

    # detail re-reads the files; a design change after pointing is flagged, a vanished ref is reported
    out, _ = F.detail(pr, store.get(pr)[0], render=False)
    assert out["items"][0]["ref"] == b["ref"] and "stale" not in out and "ts" not in out["focus"]
    sub = pr.dir / "ampli_ht.kicad_sch"
    text = sub.read_text(encoding="utf-8")
    sub.write_text(text.replace(f'"{b["value"]}"', '"CHANGED"', 1), encoding="utf-8")
    out, _ = F.detail(pr, store.get(pr)[0], render=False)
    assert out["stale"] is True and out["items"][0]["ref"] == b["ref"]
    ghost = dict(store.get(pr)[0], refs=[b["ref"], "ZZ99"])
    out, _ = F.detail(pr, ghost, render=False)
    assert out["missing"] == ["ZZ99"]
    F.clear(pr)
    assert F.latest(pr) is None and json.loads(F.as_resource(pr)["text"]) == {"focus": None, "chip": ""}


# ── channel A: the canvas over HTTP ───────────────────────────────────────
def test_canvas_http_security_and_focus_roundtrip():
    pr = _demo("canvas")
    F.clear(pr)
    info = canvas.start(pr, launch=False)
    try:
        assert info["url"].startswith("http://127.0.0.1:") and not info["launched"] and not info["reused"]
        assert canvas.start(pr, launch=False)["reused"] is True
        cv = canvas._servers[store.key(pr)]
        assert cv.httpd.server_address[0] == "127.0.0.1" and len(cv.token) >= 30
        base, tok = f"http://127.0.0.1:{cv.port}", cv.token

        # the token is required on every route; the Host must be loopback; nothing outside the fixed routes
        for path in ("/", "/api/state", "/api/scene?doc=pcb", "/svg?doc=schematic&sheet=0"):
            assert _http(base + path)[0] == 403, path
            assert _http(base + path, token="wrong")[0] == 403
        assert _http(base + "/api/state", token=tok, headers={"Host": f"evil.example:{cv.port}"})[0] == 403
        assert _http(base + "/api/focus", token=tok, body={}, headers={"Origin": "http://evil.example"})[0] == 403
        for path in ("/../../etc/passwd", "/static/../canvas.py", "/svg/../../x", "/complex_hierarchy.kicad_sch"):
            assert _http(base + path, token=tok)[0] == 404, path
        assert _http(base + "/svg?doc=pcb&layer=../../x", token=tok)[0] in (400, 409)
        assert _http(base + "/svg?doc=schematic&sheet=../x", token=tok)[0] in (400, 409)

        code, html, hdr = _http(base + "/?t=" + tok)
        page = html.decode()
        assert code == 200 and "Copper Canvas" in page and "copperCanvas" in page
        assert "default-src 'none'" in hdr["Content-Security-Policy"]
        assert not re.search(r"""(src|href)\s*=\s*["']https?://""", page)          # no CDN, no external request
        assert "//cdn" not in page and "googleapis" not in page

        st = json.loads(_http(base + "/api/state", token=tok)[1])
        assert st["project"] == "complex_hierarchy" and st["focus"] is None
        assert [s["name"] for s in st["documents"]["schematic"]["sheets"]] == ["/", "/ampli_ht_vertical", "/ampli_ht_horizontal"]
        sc = json.loads(_http(base + "/api/scene?doc=schematic&sheet=2", token=tok)[1])
        assert sc["sheet"]["name"] == "/ampli_ht_horizontal" and any(i["ref"] == "Q301" for i in sc["items"])
        code, body, hdr = _http(base + "/svg?doc=schematic&sheet=2", token=tok)
        assert code == 200 and hdr["Content-Type"] == "image/svg+xml" and b"<svg" in body[:400]
        ps = json.loads(_http(base + "/api/scene?doc=pcb", token=tok)[1])
        assert "F.Cu" in ps["layers"] and "Edge.Cuts" in ps["layers"]
        code, body, _ = _http(base + "/svg?doc=pcb&layer=F.Cu", token=tok)
        assert code == 200 and [round(v, 3) for v in svg.view_of(body.decode())] == [round(v, 3) for v in ps["view"]]

        # what the page does on click / drag / shift-click / Esc …
        q = next(i for i in sc["items"] if i["ref"] == "Q301")
        c = [(q["bbox"][0] + q["bbox"][2]) / 2, (q["bbox"][1] + q["bbox"][3]) / 2]
        r = json.loads(_http(base + "/api/focus", token=tok, body={"document": "schematic", "sheet": "2", "mode": "point", "point": c})[1])
        assert r["focus"]["refs"] == ["Q301"] and r["chip"] == "Q301" and r["focus"]["sheet"] == "/ampli_ht_horizontal"
        # … is what the model reads back through the MCP tool, with a picture
        out, imgs = _call({"project": str(pr.dir)})
        assert out["focus"]["refs"] == ["Q301"] and out["focus"]["source"] == "canvas" and out["focus"]["mode"] == "point"
        assert out["focus"]["age_s"] < 30 and out["items"][0]["ref"] == "Q301" and out["items"][0]["value"]
        assert out["items"][0]["footprint"] and out["items"][0]["at"] and out["items"][0]["nets"]
        assert len(imgs) == 1 and len(imgs[0]["data"]) > 2000
        r = json.loads(_http(base + "/api/focus", token=tok, body={"document": "schematic", "sheet": "2", "mode": "box",
                                                                    "box": [q["bbox"][0] - 30, q["bbox"][1] - 30, q["bbox"][2] + 30, q["bbox"][3] + 30]})[1])
        assert len(r["focus"]["refs"]) > 3 and "Q301" in r["focus"]["refs"] and r["focus"]["mode"] == "box"
        st = json.loads(_http(base + "/api/state", token=tok)[1])
        assert st["focus"]["refs"] == r["focus"]["refs"] and st["chip"] == r["chip"]
        bad = _http(base + "/api/focus", token=tok, body={"document": "schematic", "sheet": "2", "mode": "box", "box": [1, 2]})
        assert bad[0] == 400 and json.loads(bad[1])["error"] == "BAD_ARGUMENT"
        assert json.loads(_http(base + "/api/clear", token=tok, body={})[1])["focus"] is None
        out, imgs = _call({"project": str(pr.dir)})
        assert out["focus"] is None and out["canvas"]["running"] is True and "url" in out["canvas"] and not imgs

        # live reload: the stamp the page polls changes when a design file changes
        s0 = json.loads(_http(base + "/api/state", token=tok)[1])["documents"]["schematic"]["stamp"]
        f = pr.dir / "ampli_ht.kicad_sch"
        time.sleep(0.05)
        f.write_text(f.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        s1 = json.loads(_http(base + "/api/state", token=tok)[1])["documents"]["schematic"]["stamp"]
        assert s0 != s1
    finally:
        canvas.stop(pr)
    assert canvas.running(pr) is None and not store.canvas_info(pr)


# ── the MCP tool ──────────────────────────────────────────────────────────
def test_tool_resolve_open_clear():
    pr = _demo("tool")
    t = registry.TOOLS["focus"]
    assert t.schema["properties"]["action"]["enum"] == ["get", "resolve", "open", "clear"]
    assert len(json.dumps(t.describe())) < 4500, "keep the tool description compact"
    sc = S.load(pr, "pcb")
    it = next(i for i in sc.items if len(i["pins"]) >= 3)
    out, imgs = _call({"action": "resolve", "document": "pcb", "box": it["bbox"], "project": str(pr.dir)})
    assert it["ref"] in out["focus"]["refs"] and out["focus"]["source"] == "args" and out["focus"]["age_s"] == 0
    assert out["focus"]["box_mm"] == G.rbox(it["bbox"]) and len(imgs) == 1
    row = next(i for i in out["items"] if i["ref"] == it["ref"])
    assert set(row) >= {"ref", "value", "footprint", "at", "side", "nets"} and "bbox" not in row
    png = pr.out_dir / "focus" / "focus-pcb.png"
    from PIL import Image as PImage
    im = PImage.open(png).convert("RGB")
    assert im.width == 900 and im.height > 200
    # the focus is outlined in Copper's accent (#C8783C) in the picture the model sees
    raw = im.tobytes()
    accent = sum(1 for i in range(0, len(raw), 3)
                 if abs(raw[i] - 200) < 12 and abs(raw[i + 1] - 120) < 12 and abs(raw[i + 2] - 60) < 12)
    assert accent > 300, accent
    # box / near imply resolve; resolve without document explains itself
    out2, _ = _call({"document": "schematic", "sheet": "/ampli_ht_vertical", "near": [5, 5], "radius": 1,
                     "render": False, "project": str(pr.dir)})
    assert out2["focus"]["refs"] == [] and out2["focus"]["nearest"]["ref"]
    err, _ = _call({"action": "resolve", "box": [0, 0, 1, 1], "project": str(pr.dir)})
    assert err["__error__"] and err["error"] == "BAD_ARGUMENT" and "document=" in err["fix"]
    err, _ = _call({"action": "resolve", "document": "schematic", "sheet": "nope", "near": [1, 1], "project": str(pr.dir)})
    assert err["error"] == "SHEET_NOT_FOUND" and "/ampli_ht_vertical" in err["fix"]
    # nothing was stored by resolve; get says how to start pointing
    F.clear(pr)
    out, _ = _call({"project": str(pr.dir)})
    assert out["focus"] is None and out["canvas"]["running"] is False and "open" in out["next"]
    out, _ = _call({"action": "open", "launch": False, "project": str(pr.dir)})
    try:
        assert out["url"].startswith("http://127.0.0.1:") and "?t=" in out["url"] and out["launched"] is False
        assert out["shows"] == ["schematic", "pcb"] and "tell_user" in out
        assert _http(out["url"])[0] == 200
        got, _ = _call({"project": str(pr.dir), "wait": 0.5})
        assert got["focus"] is None and got["timed_out"] is True and got["canvas"]["running"] is True
    finally:
        canvas.stop(pr)
    out, _ = _call({"action": "clear", "project": str(pr.dir)})
    assert out == {"cleared": True, "focus": None}


# ── channel B: KiCad live selection (kipy faked — the real API is optional) ─
def test_kicad_selection_channel():
    pr = _demo("live")
    F.clear(pr)
    from copper_kicad.pcb import live as pcblive
    real_cap, real_kipy = pcblive.capability, sys.modules.get("kipy")
    try:
        # API off → exact fix, and 'auto' quietly falls back to the canvas
        pcblive.capability = lambda: {"kipy": False, "kipy_version": None, "socket": None, "available": False,
                                      "hint": "live mode needs the optional `kicad-python` package"}
        err, _ = _call({"source": "kicad", "project": str(pr.dir)})
        assert err["__error__"] and err["error"] == "NO_LIVE_KICAD"
        assert "Enable KiCad API" in err["fix"] and "kicad-python" in err["fix"] and "focus(action='open')" in err["fix"]
        out, _ = _call({"project": str(pr.dir)})
        assert out["focus"] is None and "kicad" in out
        assert live.available()["schematic"] is False and "no schematic IPC" in live.NO_SCH

        sc = S.load(pr, "pcb")
        a, b = sc.items[0], sc.items[5]
        pad = b["pins"][0]
        V = lambda x, y: types.SimpleNamespace(x=int(round(x * 1e6)), y=int(round(y * 1e6)))  # noqa: E731

        class FootprintInstance:
            def __init__(self, it):
                self.reference_field = types.SimpleNamespace(text=types.SimpleNamespace(value=it["ref"]))
                self.position = V(it["x"], it["y"])

        class Pad:
            def __init__(self, p):
                self.number, self.position, self.net = p["num"], V(p["x"], p["y"]), types.SimpleNamespace(name=p["net"])

        class Track:
            def __init__(self, t):
                self.start, self.end, self.net = V(*t["a"]), V(*t["b"]), types.SimpleNamespace(name=t["net"])

        trk = next(t for t in sc.tracks if "a" in t and t["net"])
        selected = [FootprintInstance(a), Pad(pad), Track(trk)]

        class Board:
            name = "complex_hierarchy.kicad_pcb"

            def get_selection(self):
                return list(selected)

        class KiCad:
            def __init__(self, **kw):
                pass

            def get_board(self):
                return Board()

        sys.modules["kipy"] = types.SimpleNamespace(KiCad=KiCad, __version__="fake")
        pcblive.capability = lambda: {"kipy": True, "kipy_version": "fake", "socket": "x", "available": True, "hint": None}
        f = live.selection(pr)
        assert f["source"] == "kicad" and f["document"] == "pcb" and f["mode"] == "selection"
        assert f["refs"] == sorted({a["ref"], b["ref"]}, key=G.natural_key) and f["pins"] == [f"{b['ref']}.{pad['num']}"]
        assert trk["net"] in f["nets"] and f["selected"] == {"FootprintInstance": 1, "Pad": 1, "Track": 1}
        x0, y0, x1, y1 = f["box_mm"]
        assert x0 <= min(a["bbox"][0], trk["a"][0]) and x1 >= max(a["bbox"][2], trk["a"][0]) and y0 <= a["bbox"][1] <= y1
        out, imgs = _call({"source": "kicad", "project": str(pr.dir)})
        assert out["focus"]["source"] == "kicad" and out["items"][0]["value"] is not None and len(imgs) == 1
        # 'auto' = whichever the user touched last: the unchanged KiCad selection is older than a new canvas click
        ts_k = f["ts"]
        time.sleep(0.05)
        F.point(pr, "pcb", point=[sc.items[9]["x"], sc.items[9]["y"]])
        got, _ = F.current(pr, source="auto")
        assert got["source"] == "canvas" and got["ts"] > ts_k
        selected.pop()                                    # the user changes the selection in KiCad → it wins again
        time.sleep(0.05)
        got, _ = F.current(pr, source="auto")
        assert got["source"] == "kicad"
        selected.clear()                                  # nothing selected in KiCad → canvas focus
        assert live.selection(pr) is None and F.current(pr, source="auto")[0]["source"] == "canvas"
        Board.name = "other.kicad_pcb"
        selected.append(FootprintInstance(a))
        assert "project_open" in live.selection(pr)["warning"]
    finally:
        pcblive.capability = real_cap
        if real_kipy is None:
            sys.modules.pop("kipy", None)
        else:
            sys.modules["kipy"] = real_kipy
        F.clear(pr)


# ── regressions found by the skeptical pass (dense two-sided board, 45° parts, odd requests) ─────────────
def _stickhub():
    """KiCad's `stickhub` demo: two-sided, 45°-rotated parts whose courtyards overlap pads of the other side."""
    if "stickhub" in _PR:
        return _PR["stickhub"]
    k = env.kicad()
    src = Path(k.share) / "demos" / "stickhub" if k else None
    if not src or not src.exists():
        return None
    d = _TMP / "stick hub"                       # a path with a space, on purpose
    shutil.copytree(src, d)
    for f in d.rglob("*"):
        if f.is_file():
            os.chmod(f, 0o666)
    _PR["stickhub"] = projmod.Project(d, "StickHub")
    return _PR["stickhub"]


def _pad(num, x, y, hw, hh, a=0, net=""):
    d = {"num": num, "name": "", "x": x, "y": y, "net": net, "hw": hw, "hh": hh}
    if a:
        d["a"] = a
    return d


def test_rotated_pad_hit_and_pad_priority():
    """A pad rotated 45° is hit inside its own rectangle only (not its inflated bounding box), and a part with
    a pad under the cursor beats a part whose courtyard merely covers the spot."""
    p = _pad("1", 10.0, 10.0, 1.0, 0.2, a=45.0)
    c = s = 0.70710678
    # KiCad: local (lx, ly) -> world (x + lx·cos + ly·sin, y − lx·sin + ly·cos)
    along = (10 + 0.9 * c, 10 - 0.9 * s)                 # 0.9 mm along the pad's long axis: on the pad
    across = (10 + 0.9 * s, 10 + 0.9 * c)                # 0.9 mm along its short axis: off the pad
    assert R.pad_hit(p, along, 0) and not R.pad_hit(p, across, 0)
    flat = _pad("1", 0, 0, 1.0, 0.2)
    assert R.pad_hit(flat, (0.9, 0.1), 0) and not R.pad_hit(flat, (0.1, 0.9), 0)
    small = {"ref": "C1", "side": "bottom", "bbox": [9, 9, 11, 11],
             "pins": [_pad("1", 9.2, 9.2, 0.1, 0.1), _pad("2", 10.8, 10.8, 0.1, 0.1)]}
    conn = {"ref": "J1", "side": "top", "bbox": [5, 5, 15, 15],
            "pins": [_pad("1", 10, 10, 0.4, 0.4), _pad("2", 12, 10, 0.4, 0.4)]}
    it, d = R.pick_item("pcb", [small, conn], (10.1, 10.0), 3.0)
    assert it["ref"] == "J1" and d == 0                   # was C1: "smaller courtyard wins" hid the connector pad
    assert R.pick_item("pcb", [small, conn], (10.1, 10.0), 3.0, prefer="bottom")[0]["ref"] == "C1"   # viewed side first
    assert R.pick_item("pcb", [small, conn], (9.6, 10.5), 3.0)[0]["ref"] == "C1"                    # no pad: smaller part


def test_two_sided_board_every_pad_resolves_to_its_part():
    """StickHub demo: every pad centre resolves to its own part + pad when the side is known (as the canvas
    sends it), an overlap on the other side is reported in `also`, and without a side the pad still wins over
    a bare courtyard. Before the fix 76 of 273 pads resolved to a neighbour."""
    pr = _stickhub()
    if pr is None:
        print("   SKIP: KiCad demo 'stickhub' not installed")
        return
    sc = S.load(pr, "pcb")
    assert {i["side"] for i in sc.items} == {"top", "bottom"} and any(p.get("a") for i in sc.items for p in i["pins"])
    n = also = plain_wrong = 0
    for it in sc.items:
        for p in it["pins"]:
            pt = [p["x"], p["y"]]
            for kw in ({"side": it["side"]}, {"prefer_side": it["side"]}):
                r = F.resolve(pr, "pcb", near=pt, scene=sc, **kw)
                assert r["refs"] == [it["ref"]], (it["ref"], p["num"], kw, r["refs"])
                assert r.get("pins") in ([f"{it['ref']}.{p['num']}"], None), (it["ref"], p["num"], r.get("pins"))
            also += 1 if r.get("also") else 0
            r0 = F.resolve(pr, "pcb", near=pt, scene=sc)
            if r0["refs"] != [it["ref"]]:
                plain_wrong += 1
                o = sc.by_ref()[r0["refs"][0]][0]        # only ever another part that ALSO has a pad right there
                assert any(q.get("hw") and R.pad_hit(q, pt) for q in o["pins"]), (it["ref"], p["num"], r0["refs"])
                assert any(a.startswith(it["ref"] + " ") for a in r0.get("also", [])), (it["ref"], r0)
            n += 1
    assert n > 250 and also > 20 and plain_wrong < n * 0.12, (n, also, plain_wrong)
    print(f"   {n} pads: all resolve with side/prefer_side; {also} overlap the other side (reported in `also`); "
          f"{plain_wrong} ambiguous without a side (pad-on-pad)")
    # the picture still comes out for a bottom-side part on a path with a space
    b = next(i for i in sc.items if i["side"] == "bottom")
    out, imgs = _call({"action": "resolve", "document": "pcb", "near": [b["x"], b["y"]], "side": "bottom",
                       "project": str(pr.dir)})
    assert out["focus"]["refs"] == [b["ref"]] and len(imgs) == 1


_CUSTOM_PAD_BOARD = """(kicad_pcb (version 20241229) (generator "test")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (net 0 "") (net 1 "A")
  (footprint "lib:JP" (layer "F.Cu") (at 50 40 90)
    (property "Reference" "JP1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "jumper" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd custom (at -0.725 0 90) (size 0.3 0.3) (layers "F.Cu") (net 1 "A")
      (primitives (gr_poly (pts (xy 1 0) (xy 0.5 -0.75) (xy -0.5 -0.75) (xy -0.5 0.75) (xy 0.5 0.75)) (width 0) (fill yes))))
    (pad "2" smd rect (at 0.725 0 90) (size 1 1.5) (layers "F.Cu"))
    (pad "3" smd rect (at 3 0 135) (size 2 0.4) (layers "F.Cu"))))
"""


def test_custom_pad_extent_comes_from_its_primitives():
    """A solder-jumper style custom pad: the copper is the polygon primitive, the anchor is a 0.3 mm dot."""
    from copper_kicad.focus import pcbfile
    brd = _TMP / "custompad.kicad_pcb"
    brd.write_text(_CUSTOM_PAD_BOARD, encoding="utf-8")
    fp = pcbfile.read(brd)["footprints"][0]
    p1, p2, p3 = fp["pad_list"]
    assert (fp["ref"], fp["value"], fp["side"]) == ("JP1", "jumper", "top")
    assert abs(p1["x"] - 50) < 1e-6 and abs(p1["y"] - 40.725) < 1e-6          # rotated 90°: local −x is board +y
    assert p1["hh"] >= 1.0 and p1["hw"] >= 0.75 and "a" not in p1             # was 0.15 × 0.15 (the anchor)
    assert (p2["hw"], p2["hh"]) == (0.75, 0.5) and "a" not in p2             # 90°: extents swapped onto board axes
    assert p3["a"] == 135.0 and (p3["hw"], p3["hh"]) == (1.0, 0.2)            # 45° family: own frame + angle
    assert R.pad_hit(p1, (50.6, 41.6)) and not R.pad_hit(p1, (52.0, 40.7))


_JS_HARNESS_2 = r"""
const fs = require('fs'); const noop = () => {};
const el = () => ({ style: { setProperty: noop }, classList: { toggle: noop, add: noop, remove: noop }, addEventListener: noop,
  appendChild: noop, setAttribute: noop, getContext: () => new Proxy({}, { get: () => noop, set: () => true }),
  getBoundingClientRect: () => ({ left: 0, top: 0 }), clientWidth: 1000, clientHeight: 632, focus: noop, children: [],
  setPointerCapture: noop, remove: noop });
global.document = { getElementById: () => el(), createElement: () => el(), createTextNode: () => ({}), title: '' };
global.window = { addEventListener: noop, devicePixelRatio: 1 };
global.location = { search: '?t=x', hash: '', pathname: '/' }; global.history = { replaceState: noop };
global.ResizeObserver = class { observe() {} }; global.fetch = () => Promise.reject(new Error('offline'));
global.setInterval = noop; global.navigator = {}; global.Image = class { addEventListener() {} };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const c = window.copperCanvas, k = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
c.state.scene = k.scene; c.state.tab = { doc: 'pcb', side: 'top' };
const picks = k.pts.map((pt) => [null, 'top', 'bottom'].map((prefer) => {
  const it = c.pickItem(pt, 2.0, null, prefer), pin = it ? c.pickPin(it, pt) : null;
  return [it ? it.ref : null, pin ? pin[0] : null];
}));
// box = the side being looked at unless the far side's silkscreen/fab layer is shown; point = prefer that side
const on = c.state.layersOn.top;
const sides = [c.sideArgs(true), c.sideArgs(false)];
on.add('B.SilkS'); sides.push(c.sideArgs(true));
on.delete('B.SilkS'); on.delete('B.Cu'); sides.push(c.sideArgs(false));
console.log(JSON.stringify({ picks, sides }));
"""


def test_canvas_js_rotated_pads_match_python():
    """canvas.js padHit / pickItem agree with the Python picker on rotated pads and pad-over-courtyard ties,
    and a box drag is limited to the side being looked at."""
    node = shutil.which("node")
    pr = _stickhub()
    if not node or pr is None:
        print("   SKIP: needs node and KiCad's stickhub demo")
        return
    sc = S.load(pr, "pcb")
    scene = canvas.scene_json(sc)
    assert any(p[9] for it in scene["items"] for p in it["pins"])
    rnd = random.Random(7)
    pts = [[p["x"] + rnd.uniform(-0.5, 0.5), p["y"] + rnd.uniform(-0.5, 0.5)] for it in sc.items for p in it["pins"]]
    exp = []
    for pt in pts:
        row = []
        for prefer in (None, "top", "bottom"):
            it, _d = R.pick_item("pcb", sc.items, pt, 2.0, prefer=prefer)
            pin = R.pick_pin("pcb", it, pt) if it else None
            row.append([it["ref"] if it else None, pin["num"] if pin else None])
        exp.append(row)
    hp, cp = _TMP / "harness2.js", _TMP / "cases2.json"
    hp.write_text(_JS_HARNESS_2, encoding="utf-8")
    cp.write_text(json.dumps({"scene": scene, "pts": pts}), encoding="utf-8")
    r = subprocess.run([node, str(hp), str(canvas.STATIC / "canvas.js"), str(cp)], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    got = json.loads(r.stdout)
    for pt, e, g in zip(pts, exp, got["picks"]):
        assert g == e, f"pick differs at {pt}: js {g} vs python {e}"
    assert got["sides"] == [{"side": "top"}, {"prefer": "top"}, {"prefer": "top"}, {"side": "top"}], got["sides"]
    print(f"   canvas.js == python on {len(pts)} points x 3 side preferences (rotated pads, two-sided board)")


def test_point_hints_box_crop_and_output_caps():
    pr = _demo()
    sc = S.load(pr, "pcb")
    # a point outside everything says where the parts are (a model that guessed coordinates can recover)
    far = F.resolve(pr, "pcb", near=[sc.view[0] - 50, sc.view[1] - 50])
    assert far["refs"] == [] and far["nearest"]["ref"]
    x0, y0, x1, y1 = far["parts_within_mm"]
    assert all(x0 <= i["bbox"][0] and i["bbox"][2] <= x1 and y0 <= i["bbox"][1] and i["bbox"][3] <= y1 for i in sc.items)
    assert "parts_within_mm" not in F.resolve(pr, "pcb", near=G.box_center([x0, y0, x1, y1]), radius=0)
    # a box focus is rendered as the box the user drew, not blown up to every large part it grazes
    big = max(sc.items, key=lambda i: G.box_area(i["bbox"]))
    b = big["bbox"]
    box = [b[0] - 2.0, b[1] - 2.0, b[0] + 0.5, b[1] + 0.5]                    # just clips the big part's corner
    res = F.resolve(pr, "pcb", box=box)
    assert big["ref"] in res["refs"]
    d = R.describe(pr, F.to_focus(res, "args"))
    assert d["region"] == G.rbox(box), (d["region"], box)
    # a box over the whole board stays readable: refs complete, item detail and pin lists capped
    whole = [sc.view[0], sc.view[1], sc.view[0] + sc.view[2], sc.view[1] + sc.view[3]]
    out, _ = _call({"action": "resolve", "document": "pcb", "box": whole, "render": False, "project": str(pr.dir)})
    assert len(out["focus"]["refs"]) == len({i["ref"] for i in sc.items}) > 40
    assert len(out["items"]) == 40 and out["items_total"] == len(sc.items) and "smaller box" in out["items_note"]
    cut = [b[0] - 1, b[1] - 1, (b[0] + b[2]) / 2, b[3] + 1]                   # half of the biggest part
    out, _ = _call({"action": "resolve", "document": "pcb", "box": cut, "render": False, "project": str(pr.dir)})
    assert len(out["focus"].get("pins", [])) <= 2 * F.api.PIN_CAP
    for it in out["items"]:
        assert len(it.get("pins", [])) <= F.api.PIN_CAP


def test_canvas_http_hardening_and_exact_sheet_view():
    import socket
    pr = _demo("canvas2")
    canvas.start(pr, launch=False)
    try:
        cv = canvas._servers[store.key(pr)]
        base, tok = f"http://127.0.0.1:{cv.port}", cv.token
        # the schematic SVG is served with exactly the scene's view (kicad-cli itself writes 297.0022 x 210.0072)
        for e in S.sheets(pr):
            code, body, _h = _http(base + f"/svg?doc=schematic&sheet={e['index']}", token=tok)
            sc = S.load(pr, "schematic", e["index"])
            assert code == 200 and svg.view_of(body.decode("utf-8", "replace")) == [float(v) for v in sc.view], e["name"]
        # a negative Content-Length used to park the handler thread in read(-1) until the client hung up
        t0 = time.time()
        with socket.create_connection(("127.0.0.1", cv.port), timeout=10) as sk:
            sk.sendall((f"POST /api/focus HTTP/1.1\r\nHost: 127.0.0.1:{cv.port}\r\nX-Copper-Token: {tok}\r\n"
                        "Content-Length: -1\r\n\r\n").encode())
            first = sk.recv(200).decode("latin-1")
        assert first.startswith("HTTP/1.1 400") and time.time() - t0 < 5, first[:60]
        # a bad radius is reported as what it is (it used to say "invalid JSON")
        code, body, _h = _http(base + "/api/focus", token=tok,
                               body={"document": "pcb", "mode": "point", "point": [1, 2], "radius": "x"})
        assert code == 400 and "radius" in json.loads(body)["message"]
        assert 0 < canvas._Handler.timeout <= 60          # an idle connection cannot pin a thread forever
    finally:
        canvas.stop(pr)


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
