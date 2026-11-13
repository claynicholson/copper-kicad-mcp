"""Board outline from a CAD file: STEP / STEP-Z / DXF / SVG / STL -> outline IR + report + preview.

Pure stdlib (Pillow only for the preview picture). Nothing here touches a board; the PCB tool
turns the IR into Edge.Cuts shapes and mounting holes.

    outline, report = load(path, mode='auto', ...)
    describe(outline, report)      compact dict for a tool result
    preview(outline, report, png)  the "what was picked" picture
    python -m copper_kicad.cad.outline FILE [--mode ..] [--pick N] [--png out.png]   (no board needed)
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path

from . import ir

SUPPORTED = (".step", ".stp", ".stpz", ".dxf", ".svg", ".stl")
UNSUPPORTED = {".igs": "IGES", ".iges": "IGES", ".3mf": "3MF", ".obj": "OBJ", ".dwg": "DWG", ".brep": "BREP",
               ".f3d": "Fusion archive", ".sldprt": "SolidWorks part", ".fcstd": "FreeCAD document", ".glb": "glTF",
               ".wrl": "VRML", ".x_t": "Parasolid"}
MAX_BYTES = 40 * 1024 * 1024
UNIT_MM = {"mm": 1.0, "inch": 25.4, "cm": 10.0, "m": 1000.0}


class OutlineError(Exception):
    def __init__(self, code, message, fix=None, data=None):
        Exception.__init__(self, message)
        self.code, self.message, self.fix, self.data = code, message, fix, data


def find_file(spec, project_dir=None) -> Path:
    """Resolve ``outline_file``. 'auto' = the single CAD file in <project>/mechanical/ (never the project
    root, which holds Copper's own exports of the same board)."""
    if str(spec).strip().lower() == "auto":
        mech = Path(project_dir or ".") / "mechanical"
        files = sorted(p for p in mech.glob("*") if p.is_file() and p.suffix.lower() in SUPPORTED) if mech.is_dir() else []
        if len(files) == 1:
            return files[0]
        if not files:
            raise OutlineError("OUTLINE_FILE_NOT_FOUND", f"no CAD file in {mech}",
                               f"put one {'/'.join(e[1:] for e in SUPPORTED)} file into the project's 'mechanical' folder, "
                               "or pass its path as outline_file")
        raise OutlineError("OUTLINE_AMBIGUOUS", f"{len(files)} CAD files in {mech}: {', '.join(f.name for f in files[:8])}",
                           f"pass one of them: outline_file='mechanical/{files[0].name}'")
    p = Path(os.path.expanduser(str(spec)))
    if not p.is_absolute() and project_dir:
        cand = Path(project_dir) / p
        p = cand if cand.exists() or not p.exists() else p
    if not p.is_file():
        raise OutlineError("OUTLINE_FILE_NOT_FOUND", f"outline file not found: {p}",
                           "pass an absolute path, or a path relative to the project folder")
    return p


def _check_format(path: Path):
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        what = UNSUPPORTED.get(ext)
        raise OutlineError("OUTLINE_FORMAT_UNSUPPORTED",
                           (f"{what} files ({ext}) are not supported" if what else f"'{ext}' files are not supported")
                           + f"; supported: {', '.join(SUPPORTED)}",
                           "export STEP (.step) from your CAD tool, or a DXF / SVG of the board face")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise OutlineError("OUTLINE_FILE_TOO_LARGE", f"{path.name} is {size / 1048576:.0f} MB (limit {MAX_BYTES // 1048576} MB)",
                           "export only the board or the enclosure base (not the whole assembly), or a DXF of the board face")
    return ext


def load(path, mode="auto", pick=None, axis=None, layer=None, units=None, clearance=None, flip=False,
         holes="mounting", keep_position=False, origin=(100.0, 100.0)):
    """-> (outline, report). Raises OutlineError (code, message, fix).

    outline = {'outer': loop, 'cutouts': [loop], 'holes': [{'x','y','d','size'}], 'bbox', 'area',
               'thickness', 'ignored': {...}, 'enclosure': {...}|None, 'keepouts_bottom': [loop]}
    in board coordinates (already flipped / moved to ``origin``). The clearance is NOT applied to the
    loops (pcbnew does the offset when the shapes are written); report['clearance'] carries it and
    the placement already accounts for it."""
    t0 = time.time()
    path = Path(path)
    if not path.is_file():
        raise OutlineError("OUTLINE_FILE_NOT_FOUND", f"outline file not found: {path}",
                           "pass an absolute path, or a path relative to the project folder")
    ext = _check_format(path)
    if mode not in ("auto", "board", "enclosure", "face"):
        raise OutlineError("BAD_ARGUMENT", "outline_mode must be auto, board, enclosure or face")
    if holes not in ("mounting", "all", "none"):
        raise OutlineError("BAD_ARGUMENT", "outline_holes must be mounting, all or none")
    if units is not None and units not in UNIT_MM:
        raise OutlineError("BAD_ARGUMENT", "outline_units must be mm, inch, cm or m")
    if axis is not None and str(axis).lower() not in ("+x", "-x", "+y", "-y", "+z", "-z"):
        raise OutlineError("BAD_ARGUMENT", "outline_axis must be one of +x -x +y -y +z -z")
    axis = str(axis).lower() if axis else None
    rep = {"file": path.name, "format": ext[1:], "mode": None, "ambiguous": False, "why": [], "notes": []}
    ol = None
    try:
        if ext in (".step", ".stp", ".stpz"):
            ol = _load_3d(path, "step", rep, mode, pick, axis, holes, clearance, units)
        elif ext == ".stl":
            ol = _load_3d(path, "stl", rep, mode, pick, axis, holes, clearance, units)
        else:
            ol = _load_2d(path, ext, rep, mode, layer, units, holes)
    except OutlineError:
        raise
    except Exception as e:  # noqa: BLE001
        from .step import TooLarge
        if isinstance(e, TooLarge):
            raise OutlineError("OUTLINE_FILE_TOO_LARGE", f"{path.name} unpacks to {e} (limit {MAX_BYTES // 1048576} MB)",
                               "export only the board or the enclosure base, or a DXF of the board face")
        if isinstance(e, KeyError) and ext in (".step", ".stp", ".stpz"):
            raise OutlineError("OUTLINE_PARSE_FAILED", f"{path.name} is incomplete: entity #{e.args[0]} is referenced but "
                               "missing (a truncated or damaged STEP file)", "export the file again from your CAD tool")
        raise OutlineError("OUTLINE_PARSE_FAILED", f"could not read {path.name}: {type(e).__name__}: {e}",
                           "re-export the file (STEP AP203/AP214, ASCII DXF, plain SVG), or use another format")
    if ol is None:
        if rep.get("error"):
            raise OutlineError("BAD_ARGUMENT", rep["error"], "ids: " + ", ".join(
                f"{c['id']} ({c['kind']} {c['normal']} {c['size']})" for c in rep.get("candidates", [])),
                data={"candidates": rep.get("candidates")})
        if rep.get("gaps"):
            raise OutlineError("OUTLINE_OPEN", f"the outline in {path.name} is not closed: open ends at "
                               + ", ".join(f"({g[0]}, {g[1]})" for g in rep["gaps"]),
                               "close the drawing (end points must meet within 0.01 mm), or pass outline_layer to use "
                               "another layer", data={"gaps": rep["gaps"], "layers": rep.get("layers")})
        raise OutlineError("OUTLINE_NO_PLANAR_FACE", f"no closed flat outline was found in {path.name}",
                           "export the board (or the enclosure base) as STEP with flat faces, or a DXF of the board face",
                           data={"layers": rep.get("layers")})
    cl = clearance
    if cl is None:
        cl = 0.5 if rep["mode"] == "enclosure" else 0.0
    if cl < 0 or cl > 10:
        raise OutlineError("BAD_ARGUMENT", "outline_clearance must be 0..10 mm")
    rep["clearance"] = cl
    b = ol["bbox"]
    w, h = b[2] - b[0] - 2 * cl, b[3] - b[1] - 2 * cl
    if w < 5 or h < 5:
        raise OutlineError("OUTLINE_TOO_SMALL", f"the outline is {w:.2f} x {h:.2f} mm (minimum 5 x 5)",
                           "wrong units? pass outline_units='inch' (or cm / m), or pick another face with outline_pick",
                           data={"candidates": rep.get("candidates")})
    if w > 600 or h > 600:
        raise OutlineError("OUTLINE_TOO_LARGE", f"the outline is {w:.1f} x {h:.1f} mm (maximum 600 x 600)",
                           "wrong units? pass outline_units='mm' (or inch), or pick another face with outline_pick",
                           data={"candidates": rep.get("candidates")})
    _check_nesting(ol)
    _check_simple(ol, path.name)
    # normalise: mirror, then move the (post-clearance) bbox top-left to the origin
    fx = -1.0 if flip else 1.0
    if flip:
        b = [-b[2], b[1], -b[0], b[3]]
    dx, dy = (0.0, 0.0) if keep_position else (origin[0] - (b[0] + cl), origin[1] - (b[1] + cl))
    f = lambda p: (p[0] * fx + dx, p[1] + dy)

    def tl(loop):
        return ir.transform(loop, f)
    ol["outer"] = tl(ol["outer"])
    ol["cutouts"] = [tl(c) for c in ol["cutouts"]]
    ol["keepouts_bottom"] = [tl(c) for c in ol.get("keepouts_bottom") or []]
    for hh in ol["holes"]:
        hh["x"], hh["y"] = round(hh["x"] * fx + dx, 4), round(hh["y"] + dy, 4)
    ol["bbox"] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
    ol["size"] = [round(w, 3), round(h, 3)]
    rep["flip"] = bool(flip)
    rep["seconds"] = round(time.time() - t0, 3)
    return ol, rep


def _check_nesting(ol):
    opts = ir.loop_pts(ol["outer"])
    for c in ol["cutouts"]:
        pts = ir.loop_pts(c)
        step = max(1, len(pts) // 12)
        if not all(ir.pip(opts, p[0], p[1]) for p in pts[::step]):
            raise OutlineError("OUTLINE_PARSE_FAILED", "a cutout crosses the board edge in this file",
                               "check the drawing: inner contours must lie inside the outline")


def _crossing(pts):
    """First pair of non-adjacent edges of the closed polyline that properly cross -> (x, y) or None.
    Sweep over x-sorted edges, so a clean outline of a few thousand points costs milliseconds."""
    n = len(pts)
    if n < 4:
        return None
    segs = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        segs.append((min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1]), i, a, b))
    segs.sort()

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    eps = 1e-9
    for k, (x0, x1, y0, y1, i, a, b) in enumerate(segs):
        for (u0, u1, v0, v1, j, c, d) in segs[k + 1:]:
            if u0 > x1:
                break
            if v0 > y1 or v1 < y0 or (i - j) % n in (1, n - 1):
                continue
            o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
            if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)):
                t = o3 / (o3 - o4)
                return (round(a[0] + t * (b[0] - a[0]), 3), round(a[1] + t * (b[1] - a[1]), 3))
    return None


def _check_simple(ol, name):
    """A board edge that crosses itself (a bow-tie, a figure of eight) is not a board."""
    pts = ir.loop_pts(ol["outer"])
    x = _crossing(pts)
    b = ol["bbox"]
    box = max((b[2] - b[0]) * (b[3] - b[1]), 1e-9)
    if x is None and abs(ir.poly_area(pts)) >= 1e-3 * box:
        return
    raise OutlineError("OUTLINE_SELF_INTERSECTS",
                       f"the outline in {name} crosses itself" + (f" near ({x[0]}, {x[1]})" if x else " (it encloses no area)"),
                       "fix the drawing so the board edge is one simple closed contour, or pass outline_layer / "
                       "outline_pick to use another contour", data={"at": list(x) if x else None})


def _load_3d(path, kind, rep, mode, pick, axis, holes, clearance, units):
    from . import pick as pk
    unresolved = 0
    if kind == "step":
        from . import step
        m, g, faces = step.candidates(str(path), MAX_BYTES)
        if units:
            raise OutlineError("BAD_ARGUMENT", "outline_units does not apply to STEP (the file states its unit)")
        gs = pk.groups(faces)
        unresolved = g.unresolved
        rep["planar_faces"] = len(faces)
        rep["unit_scale"] = g.scale
    else:
        from . import stl
        tris = stl.read(str(path), MAX_BYTES)
        gs = stl.groups(tris, UNIT_MM.get(units, 1.0))
        rep["triangles"] = len(tris)
        if not units:
            rep["units_assumed"] = "mm"
    if not gs:
        return None
    ol, r = pk.decide(gs, mode=mode, pick=pick, axis=axis, holes=holes, clearance=clearance, unresolved=unresolved)
    rep.update({k: v for k, v in r.items() if k not in ("why",)})
    rep["why"] += r["why"]
    rep["ambiguous"] = bool(rep["why"])
    if ol is not None and ol["ignored"].get("open_chains") and not ol["outer"]:
        return None
    return ol


def _load_2d(path, ext, rep, mode, layer, units, holes):
    if ext == ".dxf":
        from . import dxf
        try:
            d = dxf.read(str(path), layer, units)
        except dxf.DxfError as e:
            code = "OUTLINE_FORMAT_UNSUPPORTED" if "binary" in str(e) else "BAD_ARGUMENT"
            raise OutlineError(code, str(e), "pass outline_layer=<one of the listed layers>" if code == "BAD_ARGUMENT" else
                               "save the drawing as ASCII DXF")
        rep["layers"] = {k: v for k, v in list(d["layers"].items())[:12]}
        rep["used_layers"] = d["used_layers"][:8]
        prims = d["prims"]
    else:
        from . import svg
        try:
            d = svg.read(str(path), layer, units)
        except ValueError as e:
            raise OutlineError("BAD_ARGUMENT", str(e), "pass outline_layer=<a group id or label from the list>")
        prims = svg.dedupe(d["prims"])
        if d.get("ids"):
            rep["groups"] = d["ids"][:12]
    if d.get("units_assumed"):
        rep["units_assumed"] = "mm" if ext == ".dxf" else "96 dpi px"
    rep["unit_scale"] = d["scale"]
    loops, opens = ir.chain(prims)
    loops = [ir.simplify(l) for l in loops]
    ol = ir.classify(loops, holes=holes)
    big_open = None
    if opens:
        def ext_of(ch):
            pts = [q for p in ch for q in ir.prim_pts(p)]
            return (max(q[0] for q in pts) - min(q[0] for q in pts)) * (max(q[1] for q in pts) - min(q[1] for q in pts))
        big_open = max(opens, key=ext_of)
        ob = ol["bbox"] if ol else None
        if ol is None or ext_of(big_open) > 1.05 * (ob[2] - ob[0]) * (ob[3] - ob[1]):
            rep["gaps"] = ir.gaps(sorted(opens, key=ext_of, reverse=True)[:3])
            return None
    if ol is None:
        return None
    ol["ignored"]["open_chains"] = len(opens)
    rep["mode"] = "enclosure" if mode == "enclosure" else "board"
    if d.get("approx"):
        rep["notes"].append("curves_approximated")
    return ol


# ── compact description for tool results ──────────────────────────────────
def describe(ol, rep, candidates=None) -> dict:
    k = ir.kinds(ol["outer"])
    for c in ol["cutouts"]:
        for kk, v in ir.kinds(c).items():
            k[kk] += v
    hs = [{"at": [round(h["x"], 3), round(h["y"], 3)], "size": h.get("size") or f"{h.get('d')} mm"} for h in ol["holes"]]
    if len(hs) > 4:
        hs = hs[:4] + [f"... {len(hs) - 4} more"]
    out = {"file": rep["file"], "format": rep["format"], "mode": rep["mode"], "picked": rep.get("picked"),
           "size": ol.get("size"), "area_mm2": round(ol["area"], 1), "shapes": k, "cutouts": len(ol["cutouts"]),
           "holes": hs, "ignored": {a: b for a, b in ol["ignored"].items() if b},
           "thickness": ol.get("thickness"), "clearance": rep.get("clearance")}
    if rep.get("up"):
        out["viewed_from"] = rep["up"]
    if ol.get("enclosure"):
        e = ol["enclosure"]
        out["enclosure"] = {a: e[a] for a in ("cavity_depth", "seat_height", "max_height_top", "max_height_bottom") if e.get(a) is not None}
    if rep.get("units_assumed"):
        out["units_assumed"] = rep["units_assumed"]
    show = candidates if candidates is not None else rep.get("ambiguous")
    if show and rep.get("candidates"):
        out["candidates"] = rep["candidates"][:5] if candidates else rep["candidates"][:3]
    if rep.get("layers") and (candidates or rep.get("ambiguous")):
        out["layers"] = rep["layers"]
    if rep.get("why"):
        out["why"] = rep["why"][:4]
    out["seconds"] = rep.get("seconds")
    return out


def findings(ol, rep, ids_hint="") -> list:
    """Import findings in the tools' {severity, code, message, fix} shape."""
    f = []
    if rep.get("ambiguous"):
        cands = rep.get("candidates") or []
        f.append({"severity": "warning", "code": "OUTLINE_AMBIGUOUS",
                  "message": "the outline was picked with low confidence: " + "; ".join(rep["why"][:3]),
                  "fix": ("look at the picture; to use another face pass outline_pick=<id> ("
                          + ", ".join(f"{c['id']}: {c['kind']} {c['normal']} {c['size']}" for c in cands[:5]) + ")"
                          if cands else "look at the picture; pass outline_layer / outline_mode to change the choice")
                         + (", or outline_mode='board' / 'enclosure'" if cands else "")})
    if rep.get("units_assumed"):
        f.append({"severity": "info", "code": "OUTLINE_UNITS_ASSUMED",
                  "message": f"{rep['file']} states no unit: {rep['units_assumed']} assumed "
                             f"({ol['size'][0]} x {ol['size'][1]} mm)",
                  "fix": "outline_units='inch' (or mm / cm / m) if that is wrong"})
    if "curves_approximated" in rep.get("notes", []):
        f.append({"severity": "info", "code": "OUTLINE_CURVES_APPROXIMATED",
                  "message": "splines / ellipses were replaced by arcs and short segments (within about 0.01 mm)"})
    e = ol.get("enclosure")
    if e:
        top = e.get("max_height_top")
        f.append({"severity": "info", "code": "HEIGHT_LIMIT",
                  "message": (f"enclosure: cavity {e['cavity_depth']} mm deep, board seat {e['seat_height']} mm above the floor; "
                              f"components taller than {top} mm (top side, 1.6 mm board) will not fit under the rim"
                              if e.get("bosses") else
                              f"enclosure: cavity {e['cavity_depth']} mm deep; no mounting bosses were found, so the board "
                              "seat height is unknown")})
    ign = ol["ignored"]
    if ign.get("open_chains"):
        f.append({"severity": "info", "code": "OUTLINE_OPEN_PARTS_IGNORED",
                  "message": f"{ign['open_chains']} open contour(s) in the file were ignored (not part of a closed outline)"})
    return f


# ── picture ───────────────────────────────────────────────────────────────
def preview(ol, rep, png, width=900) -> str:
    """What-was-picked picture: board (green, white edge), cutouts (orange), mounting holes (cyan), dimensions."""
    from PIL import Image, ImageDraw
    b = ol["bbox"]
    bw, bh = max(b[2] - b[0], 1e-6), max(b[3] - b[1], 1e-6)
    s = min((width - 170) / bw, 760 / bh)
    W, H = width, int(bh * s + 190)
    ox, oy = (W - bw * s) / 2 + 20, 90
    im = Image.new("RGB", (W, H), (18, 20, 24))
    dr = ImageDraw.Draw(im)
    T = lambda p: (ox + (p[0] - b[0]) * s, oy + (p[1] - b[1]) * s)
    pts = [T(p) for p in ir.loop_pts(ol["outer"])]
    dr.polygon(pts, fill=(22, 78, 44))
    dr.line(pts + [pts[0]], fill=(240, 240, 240), width=3)
    for c in ol.get("keepouts_bottom") or []:
        q = [T(p) for p in ir.loop_pts(c)]
        dr.line(q + [q[0]], fill=(150, 150, 60), width=1)
    for c in ol["cutouts"]:
        q = [T(p) for p in ir.loop_pts(c)]
        dr.polygon(q, fill=(18, 20, 24))
        dr.line(q + [q[0]], fill=(255, 150, 40), width=2)
    for h in ol["holes"]:
        x, y = T((h["x"], h["y"]))
        r = max((h.get("d") or 3.2) / 2 * s, 3)
        dr.ellipse([x - r, y - r, x + r, y + r], fill=(18, 20, 24), outline=(60, 200, 255), width=3)
        dr.text((x + r + 3, y - 6), h.get("size") or ("%.1f" % (h.get("d") or 0)), fill=(60, 200, 255))
    # dimension arrows
    g = (170, 170, 170)
    y0 = oy - 22
    dr.line([(ox, y0), (ox + bw * s, y0)], fill=g)
    for x in (ox, ox + bw * s):
        dr.line([(x, y0 - 5), (x, y0 + 5)], fill=g)
    dr.text((ox + bw * s / 2 - 28, y0 - 14), "%.2f mm" % bw, fill=(230, 230, 230))
    x0 = ox - 22
    dr.line([(x0, oy), (x0, oy + bh * s)], fill=g)
    for y in (oy, oy + bh * s):
        dr.line([(x0 - 5, y), (x0 + 5, y)], fill=g)
    dr.text((max(2, x0 - 60), oy + bh * s / 2 - 6), "%.2f" % bh, fill=(230, 230, 230))
    cl = rep.get("clearance") or 0
    title = "%s  [%s]  %.2f x %.2f mm   area %.0f mm2" % (rep["file"], rep.get("mode"), bw, bh, ol["area"])
    dr.text((20, 10), title, fill=(255, 255, 255))
    ign = ol["ignored"]
    parts = ["%d mounting holes" % len(ol["holes"]), "%d cutouts" % len(ol["cutouts"])]
    if ign.get("drills"):
        parts.append("%d small drills ignored" % ign["drills"])
    if ign.get("islands"):
        parts.append("%d fused parts ignored" % ign["islands"])
    if ol.get("thickness"):
        parts.append("thickness %s mm" % ol["thickness"])
    if ol.get("enclosure"):
        parts.append("cavity depth %s mm" % ol["enclosure"]["cavity_depth"])
    if cl:
        parts.append("board edge will be %.2f mm inside this line" % cl)
    dr.text((20, 28), ",  ".join(parts), fill=(190, 190, 190))
    foot = []
    if rep.get("up"):
        foot.append("viewed from %s (component side)" % rep["up"].upper())
    if rep.get("flip"):
        foot.append("mirrored")
    if rep.get("ambiguous"):
        foot.append("LOW CONFIDENCE: " + "; ".join(rep["why"][:2]))
    if foot:
        dr.text((20, H - 44), "   ".join(foot)[:150], fill=(255, 200, 90) if rep.get("ambiguous") else (190, 190, 190))
    cands = rep.get("candidates") or []
    if cands and (rep.get("ambiguous") or rep.get("show_candidates")):
        dr.text((20, H - 24), "other faces (outline_pick=id): " + "   ".join(
            "%d: %s %s %s" % (c["id"], c["kind"], c["normal"], "x".join(str(v) for v in (c["size"] or []))) for c in cands[:5])[:150],
            fill=(150, 150, 150))
    Path(png).parent.mkdir(parents=True, exist_ok=True)
    im.save(png)
    return str(png)


# ── worker payload ────────────────────────────────────────────────────────
def to_shapes_op(ol, rep, replace=True) -> dict:
    """Arguments of the worker op ``outline_shapes`` (see pcb/workers/copper_ext.py)."""
    cl = rep.get("clearance") or 0.0
    cut = [ir.prims_to_json(c) for c in ol["cutouts"]]
    # holes with no stock footprint size become Edge.Cuts circles
    for h in ol["holes"]:
        if not h.get("size"):
            cut.append([["C", h["x"], h["y"], round((h.get("d") or 0) / 2, 4)]])
    prop = {"src": "file", "mode": rep.get("mode") or "board", "file": rep["file"][:60]}
    e = ol.get("enclosure")
    if e:
        if e.get("max_height_top") is not None:
            prop["top"] = e["max_height_top"]
        if e.get("max_height_bottom") is not None:
            prop["bottom"] = e["max_height_bottom"]
    return {"op": "outline_shapes", "outer": ir.prims_to_json(ol["outer"]), "cutouts": cut, "offset": -cl if cl else 0.0,
            "replace": replace, "expect_area": round(ol["area"], 4), "provenance": prop,
            "keepouts_bottom": [[[round(p[0], 4), round(p[1], 4)] for p in ir.loop_pts(k)] for k in ol.get("keepouts_bottom") or []]}


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="copper outline", description="Show the board outline Copper would take from a CAD file.")
    ap.add_argument("file")
    ap.add_argument("--mode", default="auto", choices=["auto", "board", "enclosure", "face"])
    ap.add_argument("--pick", type=int)
    ap.add_argument("--axis")
    ap.add_argument("--layer")
    ap.add_argument("--units")
    ap.add_argument("--clearance", type=float)
    ap.add_argument("--flip", action="store_true")
    ap.add_argument("--holes", default="mounting")
    ap.add_argument("--png")
    a = ap.parse_args(argv)
    try:
        ol, rep = load(find_file(a.file), mode=a.mode, pick=a.pick, axis=a.axis, layer=a.layer, units=a.units,
                       clearance=a.clearance, flip=a.flip, holes=a.holes)
    except OutlineError as e:
        print(json.dumps({"error": e.message, "code": e.code, "fix": e.fix, **({"data": e.data} if e.data else {})}, indent=1))
        return 1
    out = describe(ol, rep, candidates=True)
    out["findings"] = findings(ol, rep)
    if a.png:
        rep["show_candidates"] = True
        out["png"] = preview(ol, rep, a.png)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
