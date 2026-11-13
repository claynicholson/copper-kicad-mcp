"""Run the engine on ONE corpus design in a private work directory and print the result's key numbers (debugging aid).

    "<KiCad>/bin/python.exe" bench/sch/one.py rp2040-min [--keep DIR]
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

_env.setup()
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.pro import engine  # noqa: E402


def main():
    name = sys.argv[1]
    src = _env.CORPUS / name
    if not src.is_dir():
        src = _env.UNSEEN / name
    work = _env.WORK / "one" / name
    shutil.copytree(src, work)
    pros = sorted(work.glob("*.kicad_pro"))
    pr = Project(work, pros[0].stem if pros else name)
    r = engine.relayout(pr, None, force=True, style="pro2", **({} if "--nosymfix" in sys.argv else {"symfix": "auto"}))
    m = r.get("metrics") or {}
    print(json.dumps({"style": r.get("style_used"), "reason": r.get("reason"), "idioms": r.get("idioms"), "text": r.get("text"),
                      "tried": r.get("tried"),
                      "m": {k: m.get(k) for k in ("wired_local_ratio", "labels", "wire_crossings", "hard", "score",
                                                  "overlaps", "crossing_near_junction", "staggered_pairs", "decap_detached",
                                                  "decap_detached_list", "decap_rows_above", "rail_symbol_repeats",
                                                  "power_symbols", "max_junctions_on_one_power_net",
                                                  "decap_dist_mean_mm", "sheet_fill", "paper")}},
                     indent=1, default=str))
    if "--keep" in sys.argv:
        dst = Path(sys.argv[sys.argv.index("--keep") + 1])
        dst.mkdir(parents=True, exist_ok=True)
        for f in work.glob("*.kicad_sch"):
            shutil.copyfile(f, dst / f.name)
        from copper_kicad import render as R
        crop = None
        if "--crop" in sys.argv:                      # x0,y0,x1,y1 in mm
            crop = [float(v) for v in sys.argv[sys.argv.index("--crop") + 1].split(",")]
        for im in R.schematic(pr.sch, dst, width=3600, max_sheets=4):
            if crop:
                from PIL import Image
                img = Image.open(im.path).convert("RGB")
                k = img.size[0] / O.load_sheet(pr, None, create=False).page_size()[0]
                img.crop(tuple(int(v * k) for v in crop)).save(str(Path(im.path).with_suffix(".crop.png")))
            print("png", im.path)


if __name__ == "__main__":
    main()
