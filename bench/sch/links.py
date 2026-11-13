"""Label-only links of finished bench sheets (class L): parts drawn in ONE frame whose shared signal net is joined by
nothing but its name. Measures the sheets a bench run left in its output directory, so two runs can be compared:

    "<KiCad>/bin/python.exe" bench/sch/links.py bench/sch/out/_L_base bench/sch/out

per design: label_only_links (wires missing inside frames), label_only_pairs (part pairs of one frame with no wire at
all between them), wire_crossings, labels, wired_local_ratio. The numbers come from score.measure (the same code the
engine reports with); the corpus project is copied to bench/sch/.work/p<pid> and never modified.
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
from copper_kicad.sch.netlist import export_netlist  # noqa: E402
from copper_kicad.sch.pro import score  # noqa: E402
from copper_kicad.sch.pro.blocks import Design  # noqa: E402

KEYS = ("label_only_links", "label_only_part_links", "label_only_pairs", "wire_crossings", "labels", "wired_local_ratio")


def measure_dir(out_dir: Path) -> dict:
    res = {}
    for root, sub in ((_env.CORPUS, out_dir), (_env.UNSEEN, out_dir / "unseen")):
        if not root.is_dir():
            continue
        for src in sorted(d for d in root.iterdir() if d.is_dir()):
            done = sub / src.name
            schs = sorted(done.glob("*.kicad_sch")) if done.is_dir() else []
            if not schs:
                continue
            work = _env.WORK / "links" / out_dir.name / src.name
            shutil.rmtree(work, ignore_errors=True)
            shutil.copytree(src, work)
            for f in schs:
                shutil.copyfile(f, work / f.name)
            pros = sorted(work.glob("*.kicad_pro"))
            pr = Project(work, pros[0].stem if pros else src.name)
            sch = O.load_sheet(pr, None, create=False)
            nl = export_netlist(pr)
            m = score.measure(sch, nl, Design(sch, nl))
            res[src.name] = {k: m.get(k) for k in KEYS} | {"pairs": m.get("label_only_pair_list"),
                                                              "links": m.get("label_only_link_list")}
    return res


def main():
    dirs = [Path(a).resolve() for a in sys.argv[1:] if not a.startswith("--")] or [_env.OUT]
    runs = [(d.name, measure_dir(d)) for d in dirs]
    names = sorted({n for _d, r in runs for n in r})
    for k in KEYS:
        print(f"\n{k}")
        for n in names:
            print(f"  {n:22s} " + "  ".join(f"{str(r.get(n, {}).get(k)):>6s}" for _d, r in runs))
        if k != "wired_local_ratio":
            print(f"  {'TOTAL':22s} " + "  ".join(f"{sum((r.get(n, {}).get(k) or 0) for n in names):>6d}" for _d, r in runs))
    for d, r in runs:
        print(f"\npairs without any wire, {d}:")
        for n in names:
            if r.get(n, {}).get("pairs"):
                print(f"  {n:22s} {' '.join(r[n]['pairs'])}")
        print(f"\nlinks drawn as names only, {d}:")
        for n in names:
            if r.get(n, {}).get("links"):
                print(f"  {n:22s} {' '.join(r[n]['links'])}")
    if "--json" in sys.argv:
        print(json.dumps(dict(runs), indent=1))


if __name__ == "__main__":
    main()
