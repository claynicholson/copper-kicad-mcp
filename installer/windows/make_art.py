"""Draw the Windows installer's side panel and header from a real Copper board.

    python installer/windows/make_art.py                        # draw from the committed geometry
    python installer/windows/make_art.py --board macropad-rp2040.kicad_pcb   # re-extract the geometry first
    python installer/windows/make_art.py --preview big.png      # also save a 3x PNG for looking at

Writes installer/windows/assets/welcome.bmp (164 x 314, MUI2 welcome/finish panel) and header.bmp (150 x 57,
MUI2 page header), both 24-bit BMP. Deterministic: Pillow only, no design tools.

The art is the RP2040 Macropad (one of the boards made with Copper, see the README) drawn flat from its own
.kicad_pcb: board outline, front and back copper tracks, pads, vias and holes, in copper lines on light paper.
There is no text, logo or numbering in either image; NSIS draws the page text next to them.
The geometry is kept in assets/board-art.json so the art can be redrawn without the board file.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
GEOM = HERE / "assets" / "board-art.json"
PAPER = (247, 245, 240)
WHITE = (255, 255, 255)
INK = (29, 27, 24)
COPPER = (181, 101, 43)
SS = 4                      # supersampling


# ── geometry from a .kicad_pcb (pure Python, no KiCad import) ─────────────
TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def _parse(text: str):
    stack: list[list] = [[]]
    for m in TOKEN.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(t[1:-1] if t.startswith('"') else t)
    return stack[0][0]


def _kid(node, name):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def _kids(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def _xy(node):
    return (float(node[1]), float(node[2]))


def _layers(node):
    out = []
    for c in node:
        if isinstance(c, list) and c and c[0] in ("layer", "layers"):
            out += [str(v) for v in c[1:] if isinstance(v, str)]
    return out


def _rot(p, deg):
    """KiCad angles are counter-clockwise on screen (y grows downward)."""
    a = math.radians(deg)
    return (p[0] * math.cos(a) + p[1] * math.sin(a), -p[0] * math.sin(a) + p[1] * math.cos(a))


def _arc_pts(s, m, e, n=16):
    (ax, ay), (bx, by), (cx, cy) = s, m, e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [s, e]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, am, a1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, m, e))
    # walk from a0 to a1 through am
    span = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > span:
        span -= 2 * math.pi
    return [(ux + r * math.cos(a0 + span * i / n), uy + r * math.sin(a0 + span * i / n)) for i in range(n + 1)]


def extract(pcb: Path) -> dict:
    root = _parse(pcb.read_text(encoding="utf-8"))
    g: dict = {"source": pcb.name, "edge": [], "tracks": {"F.Cu": [], "B.Cu": []}, "vias": [], "pads": [], "holes": []}
    r3 = lambda p: [round(p[0], 3), round(p[1], 3)]  # noqa: E731
    for it in root[1:]:
        if not isinstance(it, list) or not it:
            continue
        tag = it[0]
        if tag in ("gr_line", "gr_arc", "gr_rect") and "Edge.Cuts" in _layers(it):
            if tag == "gr_line":
                g["edge"].append([r3(_xy(_kid(it, "start"))), r3(_xy(_kid(it, "end")))])
            elif tag == "gr_arc":
                g["edge"].append([r3(p) for p in _arc_pts(_xy(_kid(it, "start")), _xy(_kid(it, "mid")), _xy(_kid(it, "end")))])
            else:
                (x0, y0), (x1, y1) = _xy(_kid(it, "start")), _xy(_kid(it, "end"))
                g["edge"].append([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]])
        elif tag in ("segment", "arc"):
            ly = _layers(it)
            if ly and ly[0] in g["tracks"]:
                w = float(_kid(it, "width")[1])
                if tag == "segment":
                    pts = [r3(_xy(_kid(it, "start"))), r3(_xy(_kid(it, "end")))]
                else:
                    pts = [r3(p) for p in _arc_pts(_xy(_kid(it, "start")), _xy(_kid(it, "mid")), _xy(_kid(it, "end")), 8)]
                g["tracks"][ly[0]].append([round(w, 3), pts])
        elif tag == "via":
            at, size, drill = _xy(_kid(it, "at")), float(_kid(it, "size")[1]), float(_kid(it, "drill")[1])
            g["vias"].append([*r3(at), round(size / 2, 3), round(drill / 2, 3)])
        elif tag == "footprint":
            at = _kid(it, "at")
            fx, fy = _xy(at)
            fa = float(at[3]) if len(at) > 3 else 0.0
            for pad in _kids(it, "pad"):
                ly = _layers(pad)
                side = "F" if ("F.Cu" in ly or "*.Cu" in ly) else ("B" if "B.Cu" in ly else None)
                if side is None:
                    continue
                pat = _kid(pad, "at")
                lx, lyy = _xy(pat)
                pa = float(pat[3]) if len(pat) > 3 else fa
                ox, oy = _rot((lx, lyy), fa)
                cx, cy = fx + ox, fy + oy
                size = _kid(pad, "size")
                w, h = float(size[1]), float(size[2]) if len(size) > 2 else float(size[1])
                shape = pad[3] if len(pad) > 3 else "rect"
                if shape == "circle":
                    poly = [[round(cx + w / 2 * math.cos(t * math.pi / 8), 3), round(cy + w / 2 * math.sin(t * math.pi / 8), 3)] for t in range(16)]
                else:
                    corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
                    poly = [r3((cx + q[0], cy + q[1])) for q in (_rot(c, pa) for c in corners)]
                g["pads"].append([side, poly])
                drill = _kid(pad, "drill")
                if drill is not None and len(drill) > 1:
                    dval = float(drill[2]) if drill[1] == "oval" else float(drill[1])
                    if dval > 0:
                        g["holes"].append([round(cx, 3), round(cy, 3), round(dval / 2, 3)])
    xs = [p[0] for seg in g["edge"] for p in seg]
    ys = [p[1] for seg in g["edge"] for p in seg]
    g["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
    return g


# ── drawing ───────────────────────────────────────────────────────────────
def draw_board(g: dict, size: tuple[int, int], box: tuple[float, float, float, float], bg) -> Image.Image:
    """The board's top view fitted into ``box`` (x0, y0, x1, y1 in output px), at SS x."""
    W, H = size
    im = Image.new("RGBA", (W * SS, H * SS), bg + (255,))
    bx0, by0, bx1, by1 = g["bbox"]
    s = min((box[2] - box[0]) / (bx1 - bx0), (box[3] - box[1]) / (by1 - by0)) * SS
    ox = box[0] * SS + ((box[2] - box[0]) * SS - (bx1 - bx0) * s) / 2 - bx0 * s
    oy = box[1] * SS + ((box[3] - box[1]) * SS - (by1 - by0) * s) / 2 - by0 * s
    P = lambda p: (ox + p[0] * s, oy + p[1] * s)  # noqa: E731
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    def tracks(name, alpha):
        for w, pts in g["tracks"][name]:
            d.line([P(p) for p in pts], fill=COPPER + (alpha,), width=max(SS, round(w * s)), joint="curve")
            for p in (pts[0], pts[-1]):
                r = w * s / 2
                c = P(p)
                d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=COPPER + (alpha,))

    tracks("B.Cu", 80)                                    # back copper, quiet
    for side, poly in g["pads"]:
        if side == "B":
            d.polygon([P(p) for p in poly], fill=COPPER + (60,))
    im.alpha_composite(layer)
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    tracks("F.Cu", 235)
    for side, poly in g["pads"]:
        if side == "F":
            d.polygon([P(p) for p in poly], fill=COPPER + (110,), outline=COPPER + (235,), width=SS)
    for x, y, r, _ in g["vias"]:
        c = P((x, y))
        rr = r * s
        d.ellipse([c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr], fill=COPPER + (235,))
    im.alpha_composite(layer)
    d = ImageDraw.Draw(im)
    for x, y, r in g["holes"] + [[v[0], v[1], v[3]] for v in g["vias"]]:
        c = P((x, y))
        rr = max(r * s, SS * 0.6)
        d.ellipse([c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr], fill=bg + (255,))
    for seg in g["edge"]:
        d.line([P(p) for p in seg], fill=INK + (255,), width=SS, joint="curve")
    return im


def welcome(g: dict, out: Path, preview: Path | None = None) -> Path:
    W, H = 164, 314
    im = draw_board(g, (W, H), (18, 30, W - 18, H - 30), PAPER)
    small = im.resize((W, H), Image.LANCZOS).convert("RGB")
    out.parent.mkdir(parents=True, exist_ok=True)
    small.save(out, "BMP")
    if preview:
        small.resize((W * 3, H * 3), Image.NEAREST).save(preview)
    return out


def header(g: dict, out: Path, preview: Path | None = None) -> Path:
    """150 x 57, right-aligned by MUI2 on a white header: a strip of the same board's front copper."""
    W, H = 150, 57
    bx0, by0, bx1, by1 = g["bbox"]

    # draw the whole board large, then crop the busy area around the controller (upper middle of the board)
    big_w = round(W * 2.4)
    big_h = round(big_w * (by1 - by0) / (bx1 - bx0))
    im = draw_board(g, (big_w, big_h), (0, 0, big_w, big_h), WHITE).resize((big_w, big_h), Image.LANCZOS)
    cx, cy = round(big_w * 0.5), round(big_h * 0.24)
    strip = im.crop((cx - W // 2, cy - H // 2, cx - W // 2 + W, cy - H // 2 + H)).convert("RGB")
    # fade the left edge into the header's white so the page title on the left stays clean
    ramp = Image.linear_gradient("L").rotate(-90, expand=True).resize((W, H))   # 255 at the left edge, 0 at the right
    fade = ramp.point(lambda v: max(0, min(255, int((v - 90) * 2.2))))
    strip = Image.composite(Image.new("RGB", (W, H), WHITE), strip, fade)
    out.parent.mkdir(parents=True, exist_ok=True)
    strip.save(out, "BMP")
    if preview:
        strip.resize((W * 3, H * 3), Image.NEAREST).save(preview)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", help="a .kicad_pcb to (re)extract the geometry from (writes assets/board-art.json)")
    ap.add_argument("--out", default=str(HERE / "assets"))
    ap.add_argument("--preview", help="also save 3x PNGs: <preview>-welcome.png and <preview>-header.png")
    a = ap.parse_args(argv)
    if a.board:
        g = extract(Path(a.board))
        GEOM.parent.mkdir(parents=True, exist_ok=True)
        GEOM.write_text(json.dumps(g, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"geometry from {g['source']}: {sum(len(v) for v in g['tracks'].values())} tracks, "
              f"{len(g['vias'])} vias, {len(g['pads'])} pads -> {GEOM.name}")
    g = json.loads(GEOM.read_text(encoding="utf-8"))
    out = Path(a.out)
    pv = Path(a.preview) if a.preview else None
    print("wrote", welcome(g, out / "welcome.bmp", pv.with_name(pv.stem + "-welcome.png") if pv else None))
    print("wrote", header(g, out / "header.bmp", pv.with_name(pv.stem + "-header.png") if pv else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
