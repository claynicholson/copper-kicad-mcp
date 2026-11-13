"""Router-independent PNG of a board's copper (Pillow only): tracks by layer, vias, pads, missing connections."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

LAYER_COLORS = {"F.Cu": (220, 60, 50), "B.Cu": (60, 110, 230), "In1.Cu": (60, 170, 80), "In2.Cu": (230, 150, 40),
                "In3.Cu": (170, 80, 200), "In4.Cu": (60, 190, 190)}
OTHER = (150, 150, 150)


def _font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw(path, bbox, pads, outline, segments, vias, missing=(), title="", width=1800, layer_order=None):
    """segments: [[net, layer, x1, y1, x2, y2, w, arc]]; vias: [[net, x, y, size, drill, kind]];
    pads: [[x0, y0, x1, y1, side, net]]; missing: [[x1, y1, x2, y2]] (ratsnest lines still open)."""
    x0, y0, x1, y1 = bbox
    m = 1.5
    x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
    s = width / max(1e-6, x1 - x0)
    if (y1 - y0) * s > 2600:
        s = 2600 / (y1 - y0)
    W, H = int((x1 - x0) * s) + 1, int((y1 - y0) * s) + 1 + 34
    im = Image.new("RGB", (W, H), (16, 18, 22))
    d = ImageDraw.Draw(im, "RGBA")

    def P(x, y):
        return ((x - x0) * s, (y - y0) * s + 34)
    for o in outline:
        if o and o[0] == "box":
            d.rectangle([P(o[1], o[2]), P(o[3], o[4])], outline=(210, 200, 90))
        else:
            d.line([P(o[0], o[1]), P(o[2], o[3])], fill=(210, 200, 90), width=2)
    order = layer_order or sorted({sg[1] for sg in segments}, key=lambda l: (l == "F.Cu", l))
    # colours by stack position (boards name their layers freely): top red, bottom blue, inner ones in between
    stack = list(reversed(order))
    inner = [(60, 170, 80), (230, 150, 40), (170, 80, 200), (60, 190, 190)]
    colors = {}
    for i, lay in enumerate(stack):
        colors[lay] = (220, 60, 50) if i == 0 else ((60, 110, 230) if i == len(stack) - 1 else inner[(i - 1) % 4])
    top, bottom = (stack[0], stack[-1]) if stack else ("F.Cu", "B.Cu")
    for px0, py0, px1, py1, side, _net in pads:
        col = (120, 60, 60, 255) if side == top else ((50, 70, 130, 255) if side == bottom else (150, 130, 60, 255))
        d.rectangle([P(px0, py0), P(px1, py1)], fill=col)
    for lay in order:                      # F.Cu last = on top
        col = colors.get(lay, OTHER)
        for _n, l, ax, ay, bx, by, w, _arc in segments:
            if l != lay:
                continue
            pw = max(1, int(round(w * s)))
            d.line([P(ax, ay), P(bx, by)], fill=col + (215,), width=pw)
            if pw >= 3:
                r = pw / 2
                for cx, cy in (P(ax, ay), P(bx, by)):
                    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (215,))
    for _n, vx, vy, size, drill, _k in vias:
        cx, cy = P(vx, vy)
        r = max(2.0, size * s / 2)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(235, 235, 235, 255))
        r2 = max(1.0, drill * s / 2)
        d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=(16, 18, 22, 255))
    for ax, ay, bx, by in missing:
        d.line([P(ax, ay), P(bx, by)], fill=(255, 235, 60, 255), width=2)
    f = _font(18)
    d.rectangle([0, 0, W, 32], fill=(30, 33, 40, 255))
    d.text((8, 6), title, fill=(235, 235, 235), font=f)
    xk = W - 10
    for lay in reversed(order):
        tw = d.textlength(lay, font=f)
        xk -= tw + 26
        d.rectangle([xk, 9, xk + 14, 23], fill=colors.get(lay, OTHER))
        d.text((xk + 18, 6), lay, fill=(220, 220, 220), font=f)
    im.save(path)
    return path
