"""Class I (idioms) on finished sheets: the idiom metrics of score.idiom_metrics for every design of one bench run,
and optional crops of the after-PNG around every crystal, USB receptacle and regulator sense pin.

    "<KiCad>/bin/python.exe" bench/sch/idioms.py bench/sch/out/_I_base [--crops DIR] [--scale 2]

Reads <run>/<design>/<sheet>.kicad_sch (the sheets the bench copied there) and <run>/<design>/after/*.png; nothing is
written outside --crops. The netlist comes from kicad-cli on a private copy under bench/sch/.work/p<pid>."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

_env.setup()
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.netlist import export_netlist  # noqa: E402
from copper_kicad.sch.pro import score  # noqa: E402
from copper_kicad.sch.pro.blocks import Design, prefix  # noqa: E402

KEYS = ("idiom_crossings", "crystal_crossings", "crystal_grounds_max", "usbc_crossings", "usbc_pair_crossings",
        "power_x_signal", "sense_hidden", "wire_crossings", "power_sideways", "power_hooks")


def spots(D) -> list:
    """[(tag, ref, [x0, y0, x1, y1] mm)] around every crystal, USB receptacle and regulator with a sense pin."""
    out = []
    for r, us in D.units.items():
        po = D.pins_of.get(r, {})
        tag = None
        if prefix(r) in ("Y", "X") and 2 <= len(po) <= 4 and not D.is_conn(r):
            tag, pad = "crystal", 34.0
        elif D.is_conn(r) and sum(1 for p in po if str(D.pin_name.get((r, p), "")).upper().startswith("CC")) >= 2:
            tag, pad = "usbc", 42.0
        elif any(score._SENSE_PIN.match(str(D.pin_name.get((r, p), ""))) for p in po) and D.npins(r) <= 10:
            tag, pad = "sense", 45.0
        if tag:
            x, y = us[0].at[0], us[0].at[1]
            out.append((tag, r, [x - pad, y - pad, x + pad, y + pad]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--crops", default=None)
    ap.add_argument("--scale", type=float, default=2.0)
    a = ap.parse_args()
    run = Path(a.run).resolve()
    rows = {}
    for d in sorted([p for p in run.iterdir() if p.is_dir()] + [p for p in (run / "unseen").iterdir() if p.is_dir()]
                    if (run / "unseen").is_dir() else [p for p in run.iterdir() if p.is_dir()]):
        schs = sorted(d.glob("*.kicad_sch"))
        if not schs:
            continue
        work = _env.WORK / "idi" / d.name
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        for f in schs:
            shutil.copyfile(f, work / f.name)
        root = next((f for f in schs if f.stem == d.name), schs[0])
        pr = Project(work, root.stem)
        sch = O.load_sheet(pr, None, create=False)
        nl = export_netlist(pr)
        D = Design(sch, nl)
        m = score.measure(sch, nl, D)
        rows[d.name] = {k: m.get(k) for k in KEYS}
        rows[d.name]["list"] = m.get("idiom_crossing_list")
        rows[d.name]["sense_hidden_list"] = m.get("sense_hidden_list")
        print(f"{d.name:20s} " + "  ".join(f"{k.replace('_crossings', '_x')}={m.get(k)}" for k in KEYS))
        for line in m.get("idiom_crossing_list") or []:
            print("     ", line)
        if m.get("sense_hidden_list"):
            print("      sense hidden:", m["sense_hidden_list"])
        if a.crops:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            png = next(iter(sorted((d / "after").glob("*.png"))), None)
            if png is None:
                continue
            im = Image.open(png).convert("RGB")
            w_mm, h_mm = sch.page_size()
            k = im.size[0] / w_mm
            out = Path(a.crops)
            out.mkdir(parents=True, exist_ok=True)
            for tag, r, b in spots(D):
                box = (max(0, int(b[0] * k)), max(0, int(b[1] * k)), min(im.size[0], int(b[2] * k)), min(im.size[1], int(b[3] * k)))
                c = im.crop(box)
                if a.scale != 1.0:
                    c = c.resize((int(c.size[0] * a.scale), int(c.size[1] * a.scale)), Image.LANCZOS)
                c.save(out / f"{d.name}_{tag}_{r}.png")
    print(json.dumps({"totals": {k: sum((v.get(k) or 0) for v in rows.values()) for k in KEYS}}))


if __name__ == "__main__":
    main()
