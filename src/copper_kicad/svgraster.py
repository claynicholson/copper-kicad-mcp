"""Minimal, fast rasterizer for the SVG subset KiCad plotters emit (paths
M/L/H/V/A/C/Q/S/T/Z, circle, rect, line, polyline/polygon, nested <g style>).
Pillow only — no cairo, no browser. ~0.5 s for an A4 schematic."""
import math, re, sys
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw

NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
TOK = re.compile(r"[MmLlHhVvAaCcQqZzSsTt]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def parse_style(s):
    out = {}
    for part in (s or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def color(v, opacity=1.0):
    if not v or v == "none":
        return None
    v = v.strip()
    if v.startswith("#") and len(v) == 7:
        r, g, b = int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)
        return (r, g, b, int(255 * max(0.0, min(1.0, opacity))))
    if v.startswith("rgb"):
        n = [int(float(x)) for x in NUM.findall(v)[:3]]
        return (*n, int(255 * opacity))
    return (0, 0, 0, int(255 * opacity))


def arc_points(x1, y1, rx, ry, phi, fa, fs, x2, y2, n=24):
    # SVG endpoint arc -> center parameterisation (spec F.6.5)
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    phi = math.radians(phi)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        rx *= math.sqrt(lam); ry *= math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if fa == fs:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dt > 0:
        dt -= 2 * math.pi
    elif fs and dt < 0:
        dt += 2 * math.pi
    steps = max(4, int(abs(dt) / (2 * math.pi) * n * 2))
    pts = []
    for i in range(1, steps + 1):
        t = t1 + dt * i / steps
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    return pts


def path_subpaths(d):
    toks = TOK.findall(d or "")
    i, cmd = 0, None
    cx = cy = sx = sy = 0.0
    subs, cur = [], []
    closed = []

    def nums(k):
        nonlocal i
        vals = [float(t) for t in toks[i:i + k]]
        i += k
        return vals
    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t; i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy)); subs.append(cur); closed.append(True); cur = []
                cx, cy = sx, sy
                continue
        if cmd is None:
            i += 1; continue
        rel = cmd.islower()
        c = cmd.upper()
        try:
            if c == "M":
                x, y = nums(2)
                if rel: x += cx; y += cy
                if cur: subs.append(cur); closed.append(False)
                cur = [(x, y)]; cx, cy, sx, sy = x, y, x, y
                cmd = "l" if rel else "L"
            elif c == "L":
                x, y = nums(2)
                if rel: x += cx; y += cy
                cur.append((x, y)); cx, cy = x, y
            elif c == "H":
                (x,) = nums(1); x = x + cx if rel else x
                cur.append((x, cy)); cx = x
            elif c == "V":
                (y,) = nums(1); y = y + cy if rel else y
                cur.append((cx, y)); cy = y
            elif c == "A":
                rx, ry, phi, fa, fs, x, y = nums(7)
                if rel: x += cx; y += cy
                if not cur: cur = [(cx, cy)]
                cur.extend(arc_points(cx, cy, rx, ry, phi, int(fa), int(fs), x, y))
                cx, cy = x, y
            elif c in "CQST":
                k = {"C": 6, "Q": 4, "S": 4, "T": 2}[c]
                v = nums(k)
                if rel: v = [v[j] + (cx if j % 2 == 0 else cy) for j in range(k)]
                p0 = (cx, cy)
                ctrl = [(v[j], v[j + 1]) for j in range(0, k, 2)]
                if c == "S": ctrl = [p0] + ctrl
                if c == "T": ctrl = [p0] + ctrl
                pts = [p0] + ctrl
                if not cur: cur = [p0]
                for s in range(1, 13):
                    tt = s / 12
                    q = pts[:]
                    while len(q) > 1:
                        q = [((1 - tt) * a[0] + tt * b[0], (1 - tt) * a[1] + tt * b[1]) for a, b in zip(q, q[1:])]
                    cur.append(q[0])
                cx, cy = v[-2], v[-1]
            else:
                i += 1
        except ValueError:
            break
    if cur:
        subs.append(cur); closed.append(False)
    return list(zip(subs, closed))


def rasterize(svg_path, png_path, width=1600, background=None, ss=2):
    tree = ET.parse(svg_path)
    root = tree.getroot()
    vb = [float(x) for x in NUM.findall(root.get("viewBox", "0 0 100 100"))]
    vx, vy, vw, vh = vb
    W = width * ss
    scale = W / vw
    H = max(1, int(vh * scale))
    img = Image.new("RGBA", (W, H), background or (255, 255, 255, 255))
    draw = ImageDraw.Draw(img, "RGBA")

    def tx(p):
        return ((p[0] - vx) * scale, (p[1] - vy) * scale)

    def walk(el, style, offset):
        tag = el.tag.split("}")[-1]
        st = dict(style)
        st.update(parse_style(el.get("style")))
        for a in ("fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity", "opacity"):
            if el.get(a) is not None:
                st[a] = el.get(a)
        if tag in ("text", "desc", "title", "defs", "metadata"):
            return
        if st.get("opacity") == "0":
            return
        tr = el.get("transform")
        off = offset
        if tr and "translate" in tr:
            n = [float(x) for x in NUM.findall(tr.split("translate", 1)[1])[:2]]
            off = (offset[0] + n[0], offset[1] + (n[1] if len(n) > 1 else 0))
        fill = color(st.get("fill", "#000000"), float(st.get("fill-opacity", 1) or 1))
        stroke = color(st.get("stroke", "none"), float(st.get("stroke-opacity", 1) or 1))
        sw = max(1, int(round(float(NUM.findall(st.get("stroke-width", "0.1"))[0]) * scale)))
        O = lambda p: tx((p[0] + off[0], p[1] + off[1]))
        if tag == "path":
            for pts, closed in path_subpaths(el.get("d")):
                P = [O(p) for p in pts]
                if len(P) < 2:
                    continue
                if fill and (closed or st.get("fill", "none") != "none") and len(P) > 2:
                    draw.polygon(P, fill=fill)
                if stroke and sw > 0:
                    draw.line(P, fill=stroke, width=sw, joint="curve")
                    r = sw / 2
                    if r >= 1.5:
                        for q in (P[0], P[-1]):
                            draw.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=stroke)
        elif tag == "circle":
            c = O((float(el.get("cx", 0)), float(el.get("cy", 0))))
            r = float(el.get("r", 0)) * scale
            box = [c[0] - r, c[1] - r, c[0] + r, c[1] + r]
            draw.ellipse(box, fill=fill if st.get("fill", "none") != "none" else None,
                         outline=stroke, width=sw if stroke else 0)
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width", 0)), float(el.get("height", 0))
            a, b = O((x, y)), O((x + w, y + h))
            draw.rectangle([a, b], fill=fill if st.get("fill", "none") != "none" else None,
                           outline=stroke if stroke and sw > 0 else None, width=sw)
        elif tag == "line":
            a = O((float(el.get("x1", 0)), float(el.get("y1", 0))))
            b = O((float(el.get("x2", 0)), float(el.get("y2", 0))))
            if stroke:
                draw.line([a, b], fill=stroke, width=sw)
        elif tag in ("polyline", "polygon"):
            v = [float(x) for x in NUM.findall(el.get("points", ""))]
            P = [O((v[j], v[j + 1])) for j in range(0, len(v) - 1, 2)]
            if tag == "polygon" and fill and len(P) > 2:
                draw.polygon(P, fill=fill)
            if stroke and len(P) > 1:
                draw.line(P + ([P[0]] if tag == "polygon" else []), fill=stroke, width=sw)
        for ch in el:
            walk(ch, st, off)

    walk(root, {"fill": "#000000", "stroke": "none"}, (0.0, 0.0))
    if ss > 1:
        img = img.resize((W // ss, H // ss), Image.LANCZOS)
    img.convert("RGB").save(png_path, optimize=True)
    return img.size


if __name__ == "__main__":
    print(rasterize(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 1600))
