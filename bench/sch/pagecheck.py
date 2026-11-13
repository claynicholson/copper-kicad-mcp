"""Page-composition check of the sheets a bench run wrote (package D).

    python bench/sch/pagecheck.py [out dir, default bench/sch/out] [--json file]

Reads every <out>/<design>/*.kicad_sch (and <out>/unseen/<design>/...), no KiCad needed. Per sheet:
  paper        paper size
  sheet_fill   bounding box of all block frames / drawable area (the sheet inside the 10 mm drawing border)
  margins      free millimetres left / top / right / bottom of that box inside the drawing border
  balance      min(left, right) / max(left, right) and the same for top / bottom (1.0 = centred)
  thirds       share of the frame area in the top / middle / bottom third of the drawable height
  edge_min     the least distance of a frame to the drawing border or the title block
  frames       per frame: free millimetres between the frame and what is drawn in it (left, under the title,
               right, bottom) and `ink` = bounding box of the drawing / inner area of the frame
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import _env

_env.setup()

from copper_kicad import sexpr  # noqa: E402
from copper_kicad.sch.model import PAPER, Schematic  # noqa: E402
from copper_kicad.sch.pro import textbox as TB  # noqa: E402

EDGE = 10.0               # KiCad's drawing border
TITLE_W, TITLE_H = 108.0, 32.0    # the title block of the default worksheet, as drawn (measured on the renders)
STRIP = 5.08              # title strip of a block frame


def _frames(sch: Schematic) -> dict:
    rects = {str(sexpr.value(n, "uuid", "")): n for n in sch.items("rectangle")}
    out = {}
    for g in sch.items("group"):
        if len(g) > 1 and str(g[1]).startswith("copper:block:"):
            for u in (sexpr.find(g, "members") or [])[1:]:
                n = rects.get(str(u))
                if n is not None:
                    a, b = sexpr.find(n, "start"), sexpr.find(n, "end")
                    out[str(g[1])[len("copper:block:"):]] = [min(float(a[1]), float(b[1])), min(float(a[2]), float(b[2])),
                                                             max(float(a[1]), float(b[1])), max(float(a[2]), float(b[2]))]
    return out


def measure(path: Path) -> dict:
    sch = Schematic.load(path)
    pw, ph = PAPER.get(sch.paper, PAPER["A4"])
    fr = _frames(sch)
    if not fr:
        return {"paper": sch.paper, "frames": {}}
    sc = TB.scene(sch)
    x0, y0 = min(f[0] for f in fr.values()), min(f[1] for f in fr.values())
    x1, y1 = max(f[2] for f in fr.values()), max(f[3] for f in fr.values())
    dx0, dy0, dx1, dy1 = EDGE, EDGE, pw - EDGE, ph - EDGE
    m = {"left": x0 - dx0, "top": y0 - dy0, "right": dx1 - x1, "bottom": dy1 - y1}
    tb = [dx1 - TITLE_W, dy1 - TITLE_H, dx1, dy1]
    edge = min(m.values())
    for f in fr.values():
        gx = max(0.0, tb[0] - f[2], f[0] - tb[2])
        gy = max(0.0, tb[1] - f[3], f[1] - tb[3])
        edge = min(edge, (gx * gx + gy * gy) ** 0.5 if (gx and gy) else max(gx, gy))
    third = (dy1 - dy0) / 3
    tot = sum((f[2] - f[0]) * (f[3] - f[1]) for f in fr.values())
    thirds = []
    for i in range(3):
        a, b = dy0 + i * third, dy0 + (i + 1) * third
        thirds.append(round(sum((f[2] - f[0]) * max(0.0, min(f[3], b) - max(f[1], a)) for f in fr.values()) / tot, 2))
    per = {}
    for k, f in fr.items():
        boxes = []
        inside = lambda b: f[0] - 0.01 <= (b[0] + b[2]) / 2 <= f[2] + 0.01 and f[1] - 0.01 <= (b[1] + b[3]) / 2 <= f[3] + 0.01  # noqa: E731
        for t in sc["texts"]:
            if t.get("title") or not inside(t["box"]):
                continue
            boxes.append(t["box"])
        for _o, s in sc["segs"]:
            b = [min(s[0], s[2]), min(s[1], s[3]), max(s[0], s[2]), max(s[1], s[3])]
            if inside(b):
                boxes.append(b)
        for w in sc["wires"]:
            b = [max(f[0], min(w[0], w[2])), max(f[1], min(w[1], w[3])), min(f[2], max(w[0], w[2])), min(f[3], max(w[1], w[3]))]
            if b[0] <= b[2] and b[1] <= b[3] and (b[2] - b[0] + b[3] - b[1]) > 0.0:
                # a wire that only passes through (to a neighbour frame) is no content of this frame's edge
                if min(w[0], w[2]) >= f[0] - 0.01 and max(w[0], w[2]) <= f[2] + 0.01:
                    boxes.append(b)
        if not boxes:
            continue
        c = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        per[k] = {"size": [round(f[2] - f[0], 1), round(f[3] - f[1], 1)],
                  "pad": [round(c[0] - f[0], 1), round(c[1] - f[1] - STRIP, 1), round(f[2] - c[2], 1), round(f[3] - c[3], 1)],
                  "ink": round((c[2] - c[0]) * (c[3] - c[1]) / max(1.0, (f[2] - f[0]) * (f[3] - f[1] - STRIP)), 2)}
    bal = lambda a, b: round(min(a, b) / max(a, b, 0.01), 2)  # noqa: E731
    return {"paper": sch.paper,
            "sheet_fill": round((x1 - x0) * (y1 - y0) / ((dx1 - dx0) * (dy1 - dy0)), 3),
            "margins": {k: round(v, 1) for k, v in m.items()},
            "balance": {"lr": bal(m["left"], m["right"]), "tb": bal(m["top"], m["bottom"])},
            "thirds": thirds, "edge_min": round(edge, 1),
            "pad_max": round(max(max(p["pad"]) for p in per.values()), 1) if per else 0.0,
            "pad_min": round(min(min(p["pad"]) for p in per.values()), 1) if per else 0.0,
            "ink_min": min((p["ink"] for p in per.values()), default=0.0),
            "frames": per}


def main():
    argv = sys.argv[1:]
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and not (i and argv[i - 1] == "--json")]
    out = Path(args[0]) if args else _env.OUT
    res = {}
    for d in sorted(list(out.iterdir()) + list((out / "unseen").iterdir() if (out / "unseen").is_dir() else [])):
        if not d.is_dir() or d.name.startswith("_") or d.name == "unseen":
            continue
        for f in sorted(d.glob("*.kicad_sch")):
            res[d.name if f.stem == d.name else f"{d.name}/{f.stem}"] = measure(f)
    print(f"{'design':20s} {'pp':3s} {'fill':>5s}  {'left':>5s} {'top':>5s} {'right':>5s} {'bot':>5s}  {'lr':>4s} {'tb':>4s}  "
          f"{'thirds':16s} {'edge':>5s} {'padmax':>6s} {'padmin':>6s} {'inkmin':>6s}")
    for k, r in res.items():
        if not r.get("frames"):
            print(f"{k:20s} {r['paper']:3s} (no frames)")
            continue
        m = r["margins"]
        print(f"{k:20s} {r['paper']:3s} {r['sheet_fill']:5.2f}  {m['left']:5.1f} {m['top']:5.1f} {m['right']:5.1f} {m['bottom']:5.1f}  "
              f"{r['balance']['lr']:4.2f} {r['balance']['tb']:4.2f}  {str(r['thirds']):16s} {r['edge_min']:5.1f} "
              f"{r['pad_max']:6.1f} {r['pad_min']:6.1f} {r['ink_min']:6.2f}")
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(res, indent=1), encoding="utf-8")
    if "--frames" in sys.argv:
        for k, r in res.items():
            for a, p in r.get("frames", {}).items():
                print(f"  {k:20s} {a:6s} size {p['size']}  pad l/t/r/b {p['pad']}  ink {p['ink']}")


if __name__ == "__main__":
    main()
