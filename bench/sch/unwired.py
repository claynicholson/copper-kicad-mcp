"""Which same-block connections of a bench result are NOT drawn as wires (the pairs behind `wired_local_ratio`),
and where the labels of every net stand.

    "<KiCad>/bin/python.exe" bench/sch/unwired.py [design ...]

Reads bench/sch/out/<design>/*.kicad_sch (what the last bench run wrote); nothing is modified."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

_env.setup()
from copper_kicad.project import Project  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.netlist import export_netlist  # noqa: E402
from copper_kicad.sch.pro.blocks import Design  # noqa: E402


def main():
    names = sys.argv[1:] or sorted(p.name for p in _env.CORPUS.iterdir() if p.is_dir())
    for name in names:
        src = _env.CORPUS / name
        if not src.is_dir():
            src = _env.UNSEEN / name
        res = _env.OUT / name if (_env.OUT / name).is_dir() else _env.OUT / "unseen" / name
        work = _env.WORK / "unwired" / name
        shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(src, work)
        for f in res.glob("*.kicad_sch"):
            shutil.copyfile(f, work / f.name)
        pros = sorted(work.glob("*.kicad_pro"))
        pr = Project(work, pros[0].stem if pros else name)
        sch = O.load_sheet(pr, None, create=False)
        D = Design(sch, export_netlist(pr))
        grp = {}
        for gi, g in enumerate(O.geo_groups(sch)):
            for p in g["pins"]:
                grp[p] = gi
        labels = {}
        for l in sch.labels():
            labels[l["net"]] = labels.get(l["net"], 0) + 1
        print(f"== {name}: {len(sch.labels())} labels")
        lp = lw = 0
        for k, ln in D.view.nets.items():
            if k in D.rails or not D.conn(k):
                continue
            ps = ln.pins
            bad = []
            for i in range(len(ps)):
                for j in range(i + 1, len(ps)):
                    if D.block_of.get(ps[i][0]) == D.block_of.get(ps[j][0]):
                        lp += 1
                        if grp.get(ps[i]) is not None and grp.get(ps[i]) == grp.get(ps[j]):
                            lw += 1
                        else:
                            bad.append(f"{ps[i][0]}.{ps[i][1]}-{ps[j][0]}.{ps[j][1]}")
            nms = sorted(x[0] if isinstance(x, tuple) else str(x) for x in ln.names)
            nm = "/".join(nms) or str(k)
            blocks = sorted({str(D.block_of.get(p[0])) for p in ps})
            n = sum(labels.get(x, 0) for x in set(nms))
            if bad or n > 1:
                print(f"   {nm:<18} labels={n} blocks={','.join(blocks)} pins={len(ps)} unwired: {' '.join(bad)}")
        print(f"   wired_local_ratio {lw}/{lp} = {lw / max(1, lp):.3f}")


if __name__ == "__main__":
    main()
