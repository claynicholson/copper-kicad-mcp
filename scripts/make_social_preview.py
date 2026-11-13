"""Compose docs/media/social-preview.png (1280x640): GitHub's social card and the README banner.

    python scripts/make_social_preview.py <board art .png> [out.png] [--fonts DIR]

It is the same card as coppereda.com's share image (copper-platform src/lib/site/og.tsx): the site's paper
background, the repository name as the title, one plain line, the links, and the x-ray art of a real Copper-made board, generated
from its .kicad_pcb (copper-platform public/boards/<slug>/layers/xray.png). Text uses Inter (SIL OFL) from
--fonts (default: ../copper-platform/src/assets/og), falling back to a system sans-serif.
GitHub does not read the image from the repo: upload it by hand under
Settings -> General -> Social preview (see docs/internal/REPO_SETTINGS.md).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, H = 1280, 640
PAPER = (247, 245, 240)
INK = (29, 27, 24)
TEXT = (44, 42, 38)
MUTE = (105, 100, 92)
COPPER = (200, 120, 60)
NAME = "copper-kicad-mcp"
LINE = ("AI apps design real circuit", "boards in KiCad.")
SUB = ("An open-source MCP server for Claude, Codex,", "Cursor and any MCP client.")
FOOT = ("coppereda.com", "github.com/claynicholson/copper-kicad-mcp")

FALLBACK = {400: [r"C:\Windows\Fonts\segoeui.ttf", "/System/Library/Fonts/Helvetica.ttc",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
            600: [r"C:\Windows\Fonts\seguisb.ttf", "/System/Library/Fonts/Helvetica.ttc",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]}


def font(fonts: Path, weight: int, size: int) -> ImageFont.FreeTypeFont:
    for f in [fonts / f"Inter-{weight}.woff", *map(Path, FALLBACK[weight])]:
        if f.exists():
            return ImageFont.truetype(str(f), size)
    return ImageFont.load_default()


def main(argv: list[str]) -> int:
    fonts = ROOT.parent / "copper-platform" / "src" / "assets" / "og"
    if "--fonts" in argv:
        i = argv.index("--fonts")
        fonts = Path(argv[i + 1])
        del argv[i:i + 2]
    if not argv:
        print(__doc__)
        return 2
    art = Image.open(argv[0]).convert("RGBA")
    out = Path(argv[1]) if len(argv) > 1 else ROOT / "docs" / "media" / "social-preview.png"
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im)

    # art: fitted into a box on the right, as on the site card
    box_w, box_h = 520, H - 96
    s = min(box_w / art.width, box_h / art.height)
    art = art.resize((round(art.width * s), round(art.height * s)), Image.LANCZOS)
    im.paste(art, (W - 64 - box_w + (box_w - art.width) // 2, (H - art.height) // 2), art)

    # left column: the name as the title, one plain line, the links. No logo, no kicker text.
    x = 76
    y = 186
    d.text((x - 2, y), NAME, font=font(fonts, 600, 60), fill=INK)
    y += 98
    mid = font(fonts, 400, 32)
    for ln in LINE:
        d.text((x, y), ln, font=mid, fill=INK)
        y += 44
    y += 14
    sub = font(fonts, 400, 24)
    for ln in SUB:
        d.text((x, y), ln, font=sub, fill=MUTE)
        y += 34
    small = font(fonts, 400, 20)
    for i, ln in enumerate(FOOT):
        d.text((x, H - 104 + i * 28), ln, font=small, fill=MUTE)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, optimize=True)
    print(f"{out} {im.size} {out.stat().st_size / 1e3:.0f} kB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
