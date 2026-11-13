"""Text hygiene of drawn sheets: the metric `text_collisions` (copper_kicad.sch.pro.textbox) on the output of a bench
run, and a picture that proves the modelled ink boxes sit on the rendered text.

    "<KiCad>/bin/python.exe" bench/sch/textboxes.py [--out bench/sch/out] [design ...] [--overlay]

Reads <out>/<design>/<design>.kicad_sch (and <out>/unseen/...), prints every collision, and with --overlay writes
<out>/<design>/textboxes.png: the sheet's `after` PNG with every modelled text box outlined (blue: fields, green:
labels, grey: pin texts, orange: notes; RED filled: boxes in a collision). Nothing is rendered or modified."""
from __future__ import annotations

import json
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _env
    _env.setup()


def run(out_dir: Path, names: list, overlay: bool) -> dict:
    from copper_kicad.sch.model import Schematic
    from copper_kicad.sch.pro import textbox as TB
    res = {}
    dirs = [d for d in sorted(out_dir.iterdir()) if d.is_dir() and not d.name.startswith("_") and d.name != "unseen"]
    if (out_dir / "unseen").is_dir():
        dirs += sorted(d for d in (out_dir / "unseen").iterdir() if d.is_dir())
    for d in dirs:
        if names and d.name not in names:
            continue
        for f in sorted(d.glob("*.kicad_sch")):
            sch = Schematic.load(f)
            sc = TB.scene(sch)
            c = TB.collisions(sch, sc)
            res[d.name] = {k: c[k] for k in ("count", "by_kind", "lib", "nc_on_number")}
            print(f"{d.name:20s} text_collisions {c['count']:3d} {c['by_kind']}  (library: pin texts {c['lib']}, "
                  f"cross on own pin number {c['nc_on_number']})")
            for s in c["list"]:
                print("     ", s)
            png = d / "after" / (f.stem + ".png")
            if overlay and png.is_file():
                from PIL import Image, ImageDraw
                img = Image.open(png).convert("RGB")
                k = img.size[0] / sch.page_size()[0]
                dr = ImageDraw.Draw(img, "RGBA")
                col = {"field": (0, 0, 255), "power": (0, 0, 255), "label": (0, 140, 0), "pinnum": (120, 120, 120),
                       "pinname": (120, 120, 120), "note": (255, 140, 0)}
                for t in sc["texts"]:
                    b = t["box"]
                    dr.rectangle([b[0] * k, b[1] * k, b[2] * k, b[3] * k], outline=col[t["kind"]] + (255,))
                for b in c["boxes"]:
                    dr.rectangle([b[0] * k, b[1] * k, b[2] * k, b[3] * k], fill=(255, 0, 0, 90), outline=(255, 0, 0, 255))
                img.save(d / "textboxes.png")
    return res


def main():
    a = sys.argv[1:]
    out = Path(a[a.index("--out") + 1]) if "--out" in a else _env.OUT
    names = [x for i, x in enumerate(a) if not x.startswith("--") and (i == 0 or a[i - 1] != "--out")]
    res = run(out, names, "--overlay" in a)
    print(json.dumps({"total": sum(r["count"] for r in res.values()), "designs": len(res)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
