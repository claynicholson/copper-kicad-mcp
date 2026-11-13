"""Turn KiCad documents into PNGs the model can look at.

* schematic sheets: kicad-cli sch export svg → svgraster (Pillow)
* PCB layers:       kicad-cli pcb export svg → svgraster
* PCB 3D:           kicad-cli pcb render (raytraced PNG)

Optional crop (in mm, document coordinates) and max width keep token cost low.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .kicad import env
from .mcp.registry import Image, ToolError
from . import svgraster

# no F.Fab: its value/reference texts land on top of the silkscreen designators and make both unreadable
DEFAULT_PCB_LAYERS = ["F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.Mask", "Edge.Cuts"]


def _crop(png: Path, svg: Path, crop_mm: list[float] | None, pad_mm: float = 2.0, doc_box: list | None = None) -> bool:
    """Crop the PNG to crop_mm (document mm). doc_box = the document rectangle the SVG's viewBox shows
    (a PCB plotted with --fit-page-to-board starts its viewBox at 0,0 = the board's top-left corner);
    None = the viewBox is already in document coordinates (schematics)."""
    if not crop_mm:
        return False
    from PIL import Image as PImage
    import re
    vb = svg.read_text(encoding="utf-8", errors="ignore")[:4000]
    m = re.search(r'viewBox="([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)"', vb)
    if not m:
        return False
    vx, vy, vw, vh = map(float, m.groups())
    im = PImage.open(png)
    sx, sy = im.width / vw, im.height / vh
    x0, y0, x1, y1 = crop_mm
    if doc_box:
        bx0, by0, bx1, by1 = doc_box
        kx, ky = vw / max(1e-6, bx1 - bx0), vh / max(1e-6, by1 - by0)
        x0, x1 = vx + (x0 - bx0) * kx, vx + (x1 - bx0) * kx
        y0, y1 = vy + (y0 - by0) * ky, vy + (y1 - by0) * ky
        pad_mm *= kx
    box = (max(0, int((x0 - pad_mm - vx) * sx)), max(0, int((y0 - pad_mm - vy) * sy)),
           min(im.width, int((x1 + pad_mm - vx) * sx)), min(im.height, int((y1 + pad_mm - vy) * sy)))
    if box[2] - box[0] > 10 and box[3] - box[1] > 10:
        im.crop(box).save(png)
        return True
    return False


def schematic(sch: Path, out_dir: Path, width: int = 1600, crop_mm: list[float] | None = None,
              theme: str | None = None, max_sheets: int = 8, sheet: str | None = None) -> list[Image]:
    """Render every sheet of a schematic (hierarchy included), or only `sheet`
    (file stem/name, case-insensitive). `width` is the full-sheet raster width;
    callers that crop pass a proportionally larger width."""
    sch = Path(sch)
    tmp = Path(tempfile.mkdtemp(prefix="copper-svg-"))
    try:
        args = ["sch", "export", "svg", "-o", str(tmp), "--exclude-drawing-sheet" if crop_mm else "--no-background-color"]
        if theme:
            args += ["--theme", theme]
        r = env.run(args + [str(sch)], timeout=180)
        svgs = sorted(tmp.glob("*.svg"))
        if not svgs:
            # older kicad-cli lacks some flags — retry minimal
            r = env.run(["sch", "export", "svg", "-o", str(tmp), str(sch)], timeout=180)
            svgs = sorted(tmp.glob("*.svg"))
        if not svgs:
            raise ToolError(f"kicad-cli produced no SVG: {(r.stderr or r.stdout).strip()[-500:]}", code="RENDER_FAILED")
        out_dir.mkdir(parents=True, exist_ok=True)
        imgs = []
        # root sheet first
        svgs.sort(key=lambda p: (p.stem != sch.stem, p.name))
        if sheet:
            want = Path(str(sheet)).stem.lower()
            picked = [s for s in svgs if s.stem.lower() == want or s.stem.lower().endswith("-" + want)]
            picked = picked or [s for s in svgs if want in s.stem.lower()]
            if not picked:      # never fall back to another sheet silently
                raise ToolError(f"no sheet matching '{sheet}'", code="NOT_FOUND", data={"sheets": [s.stem for s in svgs]},
                                fix="render(target='schematic') without `sheet` to see all sheets")
            svgs = picked
        for svg in svgs[:max_sheets]:
            png = out_dir / f"{svg.stem}.png"
            svgraster.rasterize(str(svg), str(png), width=width)
            _crop(png, svg, crop_mm)
            imgs.append(Image(path=str(png), caption=f"schematic sheet {svg.stem}"))
        return imgs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _layer_rank(layer: str, mirror: bool) -> tuple:
    """Paint order: KiCad plots the -l list in the given order (later on top). Seen from the top, back layers
    go first, inner copper next, front layers last, the outline on top; mirrored (bottom view) the reverse."""
    import re
    near, far = ("B.", "F.") if mirror else ("F.", "B.")
    side_order = {"Fab": 0, "SilkS": 1, "Silkscreen": 1, "Paste": 2, "Mask": 3, "Cu": 4}   # within one side: Cu lowest
    if layer == "Edge.Cuts":
        return (9, 0)
    m = re.match(r"In(\d+)\.Cu$", layer)
    if m:
        n = int(m.group(1))
        return (2, -n if not mirror else n)
    kind = layer.split(".", 1)[-1]
    if layer.startswith(far):
        return (1, -side_order.get(kind, 5))
    if layer.startswith(near):
        return (3, -side_order.get(kind, 5))
    return (8, 0)


def board_layers(pcb: Path) -> list[str]:
    try:
        from .review.fab import copper_layers
        return copper_layers(Path(pcb))
    except Exception:  # noqa: BLE001
        return ["F.Cu", "B.Cu"]


def pcb_layers(pcb: Path, out_dir: Path, layers: list[str] | None = None, width: int = 1600,
               crop_mm: list[float] | None = None, mirror: bool = False, name: str = "pcb",
               auto_inner: bool = True) -> Image:
    """Plot board layers to a PNG. Layers are painted back-to-front for the viewing side (front copper is
    never hidden under a back pour). auto_inner: when both outer copper layers are asked for on a multilayer
    board, its inner copper layers are added too (a 4-layer preview without In1/In2 looks half-routed)."""
    pcb = Path(pcb)
    lay = list(dict.fromkeys(layers or DEFAULT_PCB_LAYERS))
    if auto_inner and "F.Cu" in lay and "B.Cu" in lay and not any(l.startswith("In") for l in lay):
        lay += [l for l in board_layers(pcb) if l.startswith("In")]
    lay.sort(key=lambda l: _layer_rank(l, mirror))
    tmp = Path(tempfile.mkdtemp(prefix="copper-svg-"))
    try:
        svg = tmp / "board.svg"
        args = ["pcb", "export", "svg", "-l", ",".join(lay), "--mode-single",
                "--exclude-drawing-sheet", "--fit-page-to-board", "--drill-shape-opt", "2", "-o", str(svg)]
        if mirror:
            args.append("--mirror")
        r = env.run(args + [str(pcb)], timeout=180)
        if not svg.exists():
            raise ToolError(f"kicad-cli produced no SVG: {(r.stderr or r.stdout).strip()[-500:]}", code="RENDER_FAILED")
        out_dir.mkdir(parents=True, exist_ok=True)
        png = out_dir / f"{name}.png"
        svgraster.rasterize(str(svg), str(png), width=width, background=(18, 18, 22, 255))
        cropped = False
        if crop_mm and not mirror:
            from .review.project_info import board_bbox_mm
            cropped = _crop(png, svg, crop_mm, doc_box=board_bbox_mm(pcb))
        cap = f"PCB layers {','.join(lay)} (painted in this order)" + (f", crop {list(crop_mm)} mm" if cropped else "")
        return Image(path=str(png), caption=cap)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


VIEWS = {
    "top": {"side": "top"},
    "bottom": {"side": "bottom"},
    # kicad-cli's arg parser treats a leading '-' as a flag → use 0..360 angles
    "iso": {"side": "top", "rotate": "310,0,330", "perspective": True},
    "iso_bottom": {"side": "bottom", "rotate": "50,0,30", "perspective": True},
    "front": {"side": "front"},
}


def pcb_3d(pcb: Path, out_dir: Path, view: str = "iso", width: int = 1400, height: int = 900,
           quality: str = "basic") -> Image:
    v = VIEWS.get(view, VIEWS["iso"])
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"pcb-3d-{view}.png"
    args = ["pcb", "render", "-o", str(png), "--width", str(width), "--height", str(height),
            "--side", v["side"], "--quality", quality, "--background", "opaque"]
    if v.get("perspective"):
        args.append("--perspective")
    if v.get("rotate"):
        args.append(f"--rotate={v['rotate']}")
    r = env.run(args + [str(pcb)], timeout=300)
    if not png.exists():
        # some builds parse a leading '-' as a flag; retry without rotation
        args = [a for a in args if not a.startswith("--rotate")]
        r = env.run(args + [str(pcb)], timeout=300)
    if not png.exists():
        raise ToolError(f"3D render failed: {(r.stderr or r.stdout).strip()[-500:]}", code="RENDER_FAILED")
    return Image(path=str(png), caption=f"PCB 3D {view}")
