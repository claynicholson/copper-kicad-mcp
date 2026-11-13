"""KiCad's own SVG plots, kept in true mm.

* schematic: ``kicad-cli sch export svg`` — viewBox ``0 0 <page mm>``; SVG user units are sheet mm.
* board:     ``kicad-cli pcb export svg --page-size-mode 1`` — user units are board mm (no fit-to-board
  shift), one file per layer. We only ever rewrite the root ``viewBox`` / ``width`` / ``height`` to choose
  the visible region; the drawing itself is untouched, so a point at SVG (x, y) IS KiCad (x, y) mm.

Exports are cached per file change under ``<COPPER_HOME>/focus-cache`` (never inside the project).

    schematic_svgs(pr) -> {sheet path: Path}       pcb_svgs(pr, layers) -> {layer: Path}
    with_view(svg_text, view) -> str               crop_png(pr, focus-like dict, items, out) -> Image
"""
from __future__ import annotations

import hashlib
import re
import shutil
import threading
import time
from pathlib import Path

from .. import project as projmod
from ..kicad import env
from ..mcp.registry import Image, ToolError
from . import geometry as G
from . import scene as S

ACCENT = (200, 120, 60)            # #C8783C
PCB_BG = (16, 16, 20)              # canvas background behind board layers
PAPER = (245, 244, 239)            # schematic paper
_lock = threading.Lock()

# kicad-cli names plot files after KiCad's long layer names
_LAYER_FILE = {"F.SilkS": "F_Silkscreen", "B.SilkS": "B_Silkscreen", "F.CrtYd": "F_Courtyard",
               "B.CrtYd": "B_Courtyard", "F.Adhes": "F_Adhesive", "B.Adhes": "B_Adhesive",
               "Dwgs.User": "User_Drawings", "Cmts.User": "User_Comments"}


_LAYER_ROW = re.compile(r'^\s*\(\d+\s+"([^"]+)"\s+\w+(?:\s+"([^"]*)")?\s*\)', re.M)


def layer_stem(layer: str, pcb: Path | None = None) -> str:
    """File-name part kicad-cli uses for a layer: the board's own (possibly user-renamed) layer name."""
    if pcb is not None:
        try:
            with open(pcb, encoding="utf-8", errors="replace") as f:
                head = f.read(12000)
            for canon, user in _LAYER_ROW.findall(head):
                if canon == layer and user:
                    return re.sub(r'[<>:"/\|?*.\s]', "_", user)
        except OSError:
            pass
    return _LAYER_FILE.get(layer, layer.replace(".", "_"))


def canvas_layers(copper_layers: int = 2) -> list[str]:
    """Layers the canvas offers, bottom-most first (draw order for the top view)."""
    inner = [f"In{i}.Cu" for i in range(1, max(0, copper_layers - 2) + 1)]
    return ["B.Fab", "B.SilkS", "B.Cu"] + inner[::-1] + ["F.Cu", "F.SilkS", "F.Fab", "Edge.Cuts"]


def cache_dir(pr) -> Path:
    key = hashlib.sha1(f"{str(pr.dir.resolve()).lower()}|{pr.name}".encode()).hexdigest()[:16]
    d = Path(projmod.STATE_DIR) / "focus-cache" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(root: Path, keep: Path):
    """Drop older exports of the same document (same 'sch-' / 'pcb-' prefix), keeping the previous one."""
    try:
        pre = keep.name.split("-", 1)[0] + "-"
        olds = sorted((p for p in root.iterdir() if p.is_dir() and p != keep and p.name.startswith(pre)),
                      key=lambda p: p.stat().st_mtime)
        for p in olds[:-1]:
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def schematic_svgs(pr) -> dict:
    """{sheet instance path ('/uuid/…'): svg Path} for every sheet — one kicad-cli run per schematic change."""
    st = S.stamp(pr, "schematic")
    d = cache_dir(pr) / f"sch-{st}"
    with _lock:
        if not (d / ".done").exists():
            d.mkdir(parents=True, exist_ok=True)
            r = env.run(["sch", "export", "svg", "-o", str(d), "--no-background-color", "--exclude-drawing-sheet",
                         str(pr.sch)], timeout=240)
            if not list(d.glob("*.svg")):
                r = env.run(["sch", "export", "svg", "-o", str(d), str(pr.sch)], timeout=240)
            if not list(d.glob("*.svg")):
                shutil.rmtree(d, ignore_errors=True)
                raise ToolError(f"kicad-cli produced no schematic SVG: {(r.stderr or r.stdout).strip()[-400:]}",
                                code="RENDER_FAILED", fix="open the schematic in KiCad to check it loads, then retry")
            (d / ".done").write_text(str(time.time()))
            _prune(d.parent, d)
    out, files = {}, {p.stem.lower(): p for p in d.glob("*.svg")}
    sheets = S.sheets(pr)
    for e in sheets:
        p = files.get(e["svg"].lower())
        if p is not None:
            out[e["path"]] = p
    if len(out) < len(sheets):          # unusual sheet names: fall back to KiCad's own ordering
        rest = sorted(set(files.values()) - set(out.values()))
        for e, p in zip([e for e in sheets if e["path"] not in out], rest):
            out[e["path"]] = p
    return out


def pcb_svgs(pr, layers: list[str], tag: str = "layers") -> dict:
    """{layer: svg Path}, one file per layer in true board mm — one kicad-cli run per board change."""
    st = S.stamp(pr, "pcb")
    d = cache_dir(pr) / f"pcb-{st}" / tag
    with _lock:
        if not (d / ".done").exists():
            d.mkdir(parents=True, exist_ok=True)
            r = env.run(["pcb", "export", "svg", "-l", ",".join(layers), "--mode-multi", "--exclude-drawing-sheet",
                         "--page-size-mode", "1", "--drill-shape-opt", "2", "-o", str(d), str(pr.pcb)], timeout=300)
            if not list(d.glob("*.svg")):
                shutil.rmtree(d, ignore_errors=True)
                raise ToolError(f"kicad-cli produced no board SVG: {(r.stderr or r.stdout).strip()[-400:]}",
                                code="RENDER_FAILED", fix="pcb_read() to check the board loads, then retry")
            (d / ".done").write_text(str(time.time()))
            _prune(d.parent.parent, d.parent)
    out = {}
    for ly in layers:
        p = d / f"{pr.pcb.stem}-{layer_stem(ly, pr.pcb)}.svg"
        if not p.exists():
            p = d / f"{pr.pcb.stem}-{layer_stem(ly)}.svg"
        if p.exists():
            out[ly] = p
    return out


def pcb_single(pr, layers: list[str]) -> Path:
    """One SVG with `layers` composited by KiCad (for the PNG crop the model looks at)."""
    st = S.stamp(pr, "pcb")
    tag = hashlib.sha1(",".join(layers).encode()).hexdigest()[:8]
    d = cache_dir(pr) / f"pcb-{st}"
    p = d / f"single-{tag}.svg"
    with _lock:
        if not p.exists():
            d.mkdir(parents=True, exist_ok=True)
            r = env.run(["pcb", "export", "svg", "-l", ",".join(layers), "--mode-single", "--exclude-drawing-sheet",
                         "--page-size-mode", "1", "--drill-shape-opt", "2", "-o", str(p), str(pr.pcb)], timeout=300)
            if not p.exists():
                raise ToolError(f"kicad-cli produced no board SVG: {(r.stderr or r.stdout).strip()[-400:]}",
                                code="RENDER_FAILED", fix="pcb_read() to check the board loads, then retry")
            _prune(d.parent, d)
    return p


_ROOT = re.compile(r"<svg\b[^>]*>", re.S)


def with_view(svg_text: str, view) -> str:
    """Rewrite only the root <svg> width/height/viewBox to show `view` = [x, y, w, h] mm."""
    m = _ROOT.search(svg_text)
    if not m:
        return svg_text
    x, y, w, h = (float(v) for v in view)
    tag = m.group(0)
    tag = re.sub(r'\swidth="[^"]*"', f' width="{w:.4f}mm"', tag, count=1)
    tag = re.sub(r'\sheight="[^"]*"', f' height="{h:.4f}mm"', tag, count=1)
    vb = f'viewBox="{x:.4f} {y:.4f} {w:.4f} {h:.4f}"'
    tag = re.sub(r'viewBox="[^"]*"', vb, tag, count=1) if "viewBox=" in tag else tag[:-1] + f" {vb}>"
    return svg_text[:m.start()] + tag + svg_text[m.end():]


def view_of(svg_text: str) -> list[float] | None:
    m = _ROOT.search(svg_text)
    v = re.search(r'viewBox="([^"]*)"', m.group(0)) if m else None
    if not v:
        return None
    n = [float(t) for t in re.split(r"[\s,]+", v.group(1).strip())]
    return n if len(n) == 4 else None


def dark_holes(svg_text: str) -> str:
    """kicad-cli plots drill holes white (for paper); on the dark canvas they are shown as holes."""
    return svg_text.replace("fill:#FFFFFF", "fill:#%02X%02X%02X" % PCB_BG)


# ── the picture the model looks at ────────────────────────────────────────
def _region(sc: S.Scene, region, focus: dict) -> list[float]:
    vx, vy, vw, vh = sc.view
    min_side = 36.0 if sc.document == "schematic" else 10.0
    if not region:
        return [vx, vy, vx + vw, vy + vh]
    x0, y0, x1, y1 = region
    if focus.get("mode") == "point" and focus.get("point_mm") and not focus.get("refs"):
        min_side *= 1.5
    pad = max(2.0 if sc.document == "pcb" else 6.0, 0.18 * max(x1 - x0, y1 - y0))
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    for lo, hi, i in ((x0, x1, 0), (y0, y1, 1)):
        if hi - lo < min_side:
            c = (lo + hi) / 2
            if i == 0:
                x0, x1 = c - min_side / 2, c + min_side / 2
            else:
                y0, y1 = c - min_side / 2, c + min_side / 2
    # keep a sane aspect (never a sliver)
    w, h = x1 - x0, y1 - y0
    if w > 2.2 * h:
        c = (y0 + y1) / 2
        y0, y1 = c - w / 4.4, c + w / 4.4
    elif h > 1.6 * w:
        c = (x0 + x1) / 2
        x0, x1 = c - h / 3.2, c + h / 3.2
    return [x0, y0, x1, y1]


def crop_png(pr, focus: dict, items: list, sc: S.Scene, region, out_png: Path, width: int = 900) -> Image:
    """PNG of the focused region with the focus outlined in Copper's accent: the box / point the user drew,
    each referenced part's outline and its reference. Returns a registry Image; `mm_per_px` is in the caption."""
    from PIL import Image as PImage, ImageDraw, ImageFont
    from .. import svgraster
    x0, y0, x1, y1 = _region(sc, region, focus)
    view = [x0, y0, x1 - x0, y1 - y0]
    bottom = sc.document == "pcb" and items and all(i.get("side") == "bottom" for i in items)
    if sc.document == "schematic":
        src = schematic_svgs(pr).get(sc.sheet["path"])
        if src is None:
            raise ToolError("no SVG for that sheet", code="RENDER_FAILED", fix="render(target='schematic')")
        text, bg = src.read_text(encoding="utf-8", errors="replace"), PAPER + (255,)
    else:
        layers = (["F.Cu", "B.Cu", "B.SilkS", "Edge.Cuts"] if bottom else ["B.Cu", "F.Cu", "F.SilkS", "Edge.Cuts"])
        text = dark_holes(pcb_single(pr, layers).read_text(encoding="utf-8", errors="replace"))
        bg = PCB_BG + (255,)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_png.with_suffix(".view.svg")
    tmp.write_text(with_view(text, view), encoding="utf-8")
    try:
        svgraster.rasterize(str(tmp), str(out_png), width=width, background=bg)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    im = PImage.open(out_png).convert("RGB")
    k = im.width / view[2]
    dr = ImageDraw.Draw(im, "RGBA")

    def px(x, y):
        return ((x - x0) * k, (y - y0) * k)

    try:
        font = ImageFont.load_default(size=18)
    except TypeError:                       # Pillow < 10.1
        font = ImageFont.load_default()
    halo = (16, 16, 20, 255) if sc.document == "pcb" else (255, 255, 255, 200)
    for it in items:
        b = it["bbox"]
        a, c = px(b[0], b[1]), px(b[2], b[3])
        r = [a[0] - 3, a[1] - 3, c[0] + 3, c[1] + 3]
        if it.get("pins") and focus.get("mode") != "box":     # only a pin of this part is meant: thin outline
            dr.rectangle(r, outline=ACCENT + (255,), width=1)
        else:
            dr.rectangle([r[0] - 1, r[1] - 1, r[2] + 1, r[3] + 1], outline=halo, width=5)   # halo: visible on copper
            dr.rectangle(r, outline=ACCENT + (255,), width=3, fill=ACCENT + (30,))
        for p in it.get("pins") or []:
            q = px(p["x"], p["y"])
            dr.ellipse([q[0] - 8, q[1] - 8, q[0] + 8, q[1] + 8], outline=halo, width=5)
            dr.ellipse([q[0] - 7, q[1] - 7, q[0] + 7, q[1] + 7], outline=ACCENT + (255,), width=3)
    if focus.get("box_mm") and focus.get("mode") == "box":
        b = focus["box_mm"]
        a, c = px(b[0], b[1]), px(b[2], b[3])
        _dashed(dr, [a[0], a[1], c[0], c[1]], halo, width=5)
        _dashed(dr, [a[0], a[1], c[0], c[1]], (255, 176, 112, 255), width=3)
    if focus.get("point_mm"):
        q = px(*focus["point_mm"])
        for col, w in ((halo, 5), (ACCENT + (255,), 3)):
            dr.line([q[0] - 13, q[1], q[0] + 13, q[1]], fill=col, width=w)
            dr.line([q[0], q[1] - 13, q[0], q[1] + 13], fill=col, width=w)
    placed = []
    for it in items:                         # references last, on a plate so they stay readable
        b = it["bbox"]
        a = px(b[0], b[1])
        label = it["ref"]
        tw = dr.textlength(label, font=font)
        tx, ty = max(2, min(im.width - tw - 10, a[0] - 3)), max(2, a[1] - 28)
        while any(abs(tx - ox) < ow + 4 and abs(ty - oy) < 24 for ox, oy, ow in placed) and ty + 26 < im.height:
            ty += 25                         # stack plates instead of overprinting neighbours
        placed.append((tx, ty, tw + 10))
        dr.rectangle([tx, ty, tx + tw + 10, ty + 24], fill=(22, 22, 26, 240), outline=ACCENT + (255,))
        dr.text((tx + 5, ty + 1), label, fill=(255, 196, 150, 255), font=font)
    im.save(out_png, optimize=True)
    what = "schematic " + sc.sheet["name"] if sc.document == "schematic" else "PCB (seen from the top)"
    return Image(path=str(out_png),
                 caption=f"focus on {what}: region {G.rbox([x0, y0, x1, y1])} mm, {1 / k:.3f} mm/px, focus outlined in orange")


def _dashed(dr, box, color, dash=9, gap=6, width=2):
    x0, y0, x1, y1 = box
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
        L = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        if L < 1:
            continue
        ux, uy = (bx - ax) / L, (by - ay) / L
        t = 0.0
        while t < L:
            e = min(L, t + dash)
            dr.line([ax + ux * t, ay + uy * t, ax + ux * e, ay + uy * e], fill=color, width=width)
            t += dash + gap
