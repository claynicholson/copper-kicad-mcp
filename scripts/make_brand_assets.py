"""Generate Copper brand assets with Pillow (deterministic, no design tools).

    python scripts/make_brand_assets.py [--out installer/windows/assets]

Writes:
  installer/windows/assets/welcome.bmp   164x314  MUI2 welcome/finish side panel  (via installer/windows/make_art.py:
  installer/windows/assets/header.bmp    150x57   MUI2 page header                 a real board, no text)
  installer/windows/assets/copper.ico    16…256   installer / uninstaller / shortcut icon
  mcpb/icon.png                          512x512  Claude Desktop extension icon
  plugin/assets/logo.png                 256x256  Claude Code / Codex plugin logo

The mark is a "Cu" drawn as PCB copper: 45-degree chamfered traces ending in
plated pads, accent #C8783C on #16161A.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
ACCENT = (0xC8, 0x78, 0x3C)
BG = (0x16, 0x16, 0x1A)

# "Cu" in a 100x100 design box: polylines + pad positions
C_PATH = [(46, 27), (32, 27), (22, 37), (22, 63), (32, 73), (46, 73)]
U_PATH = [(57, 44), (57, 66), (63, 72), (74, 72), (80, 66), (80, 44)]
PADS = [(46, 27), (46, 73), (57, 44), (80, 44)]
# tiny sizes: fewer, bolder strokes, no drilled pads
C_SMALL = [(48, 25), (32, 25), (20, 37), (20, 63), (32, 75), (48, 75)]
U_SMALL = [(60, 42), (60, 75), (82, 75), (82, 42)]


def draw_mark(size: int, tile=True, simplify=None, color=ACCENT, bg=BG, radius_frac=0.22, glow=False) -> Image.Image:
    """Render the Cu mark at ``size`` px (4x supersampled)."""
    ss = 4
    S = size * ss
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if tile:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * radius_frac), fill=bg + (255,))
    if tile and size >= 64:
        # faint inner bevel
        d.rounded_rectangle([ss, ss, S - 1 - ss, S - 1 - ss], radius=int(S * radius_frac) - ss,
                            outline=(255, 255, 255, 10), width=ss)
    simplify = size <= 24 if simplify is None else simplify
    k = S / 100.0
    w = (13.0 if simplify else 7.0) * k
    pad_r = 0 if simplify else 7.2 * k
    hole_r = 0 if simplify else 2.6 * k

    def P(pts):
        return [(x * k, y * k) for x, y in pts]

    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for path in ((C_SMALL, U_SMALL) if simplify else (C_PATH, U_PATH)):
        pts = P(path)
        ld.line(pts, fill=color + (255,), width=int(w), joint="curve")
        for x, y in pts[1:-1]:
            r = w / 2
            ld.ellipse([x - r, y - r, x + r, y + r], fill=color + (255,))
    if pad_r:
        for x, y in P(PADS):
            ld.ellipse([x - pad_r, y - pad_r, x + pad_r, y + pad_r], fill=color + (255,))
    if glow and not simplify:
        g = layer.filter(ImageFilter.GaussianBlur(S * 0.03))
        g.putalpha(g.getchannel("A").point(lambda a: int(a * 0.35)))
        im.alpha_composite(g)
    im.alpha_composite(layer)
    if hole_r:
        hd2 = ImageDraw.Draw(im)
        for x, y in P(PADS):
            hd2.ellipse([x - hole_r, y - hole_r, x + hole_r, y + hole_r], fill=bg + (255,) if tile else (0, 0, 0, 0))
    return im.resize((size, size), Image.LANCZOS)


def icon(path: Path):
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = [draw_mark(s, tile=True) for s in sizes]
    imgs[-1].save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    root = Path(a.root)
    inst = root / "installer" / "windows" / "assets"
    inst.mkdir(parents=True, exist_ok=True)
    # the installer's side panel and header are drawn from a real board by installer/windows/make_art.py
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_art", ROOT / "installer" / "windows" / "make_art.py")
    art = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(art)
    art.main(["--out", str(inst)])
    icon(inst / "copper.ico")
    (root / "mcpb").mkdir(exist_ok=True)
    draw_mark(512, tile=True).save(root / "mcpb" / "icon.png", optimize=True)
    (root / "plugin" / "assets").mkdir(parents=True, exist_ok=True)
    draw_mark(256, tile=True).save(root / "plugin" / "assets" / "logo.png", optimize=True)
    # previews for humans (not shipped)
    prev = Path(os.environ.get("COPPER_ASSET_PREVIEW", "")) if os.environ.get("COPPER_ASSET_PREVIEW") else None
    if prev:
        prev.mkdir(parents=True, exist_ok=True)
        for s in (16, 32, 48):
            draw_mark(s).resize((s * 8, s * 8), Image.NEAREST).save(prev / f"icon{s}x8.png")
        Image.open(inst / "welcome.bmp").resize((328, 628), Image.LANCZOS).save(prev / "welcome2x.png")
        Image.open(inst / "header.bmp").resize((450, 171), Image.LANCZOS).save(prev / "header3x.png")
    print("brand assets written under", root)


if __name__ == "__main__":
    sys.exit(main())
