"""Probe: does KiCad keep a symfix-derived embedded symbol, and does the gate accept it?

    "<KiCad>/bin/python.exe" bench/sch/symfix_probe.py [--lib-name] [design ...]

--lib-name: the variant of the design document (a separate "<name>_cu" symbol selected with lib_name). KiCad 10
writes the lib_name into the netlist, so G1 refuses it - which is why the engine replaces the embedded copy in place.

Applies symfix Level 2 to the UNTOUCHED corpus sheet (no re-layout), runs the engine gate with the symfix clause on
the staged copy and prints what the gate and ERC say. Nothing in the corpus is modified."""
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
from copper_kicad.sch.pro import gate, symfix  # noqa: E402


def main():
    by_name = "--lib-name" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    names = args or sorted(p.name for p in _env.CORPUS.iterdir() if p.is_dir())
    for name in names:
        work = _env.WORK / "probe" / name
        shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(_env.CORPUS / name, work)
        pros = sorted(work.glob("*.kicad_pro"))
        pr = Project(work, pros[0].stem if pros else name)
        sch = O.load_sheet(pr, None, create=False)
        got = symfix.apply(sch, lib_name=by_name)
        if not got:
            print(name, "- nothing to derive;", "hints:", symfix.hints(sch))
            continue
        sch.prune_lib_symbols()
        b = gate.baseline(pr, pr.sch, sch.sheet_path, with_erc=True)
        try:
            lib = {k: n for k, n in b.erc.items() if k != "error" and json.loads(k)[1] in gate.ERC_LIB_TYPES}
            g = gate.check(pr, b, sch.text(), sch.sheet_path, symfix=True)
            print(name, "derived:", {k: {x: v[x] for x in ("fanned", "offset", "refs")} for k, v in got.items()})
            print("   gate ok:", g.ok, "| reasons:", g.reasons[:4], "| netlist", g.hash_before, g.hash_after)
            print("   ERC library-symbol items before:", sum(lib.values()), "| sample:", list(lib)[:2])
            g2 = gate.check(pr, b, sch.text(), sch.sheet_path, symfix=False)
            print("   without the clause the same candidate is refused:", not g2.ok, g2.reasons[:1])
        finally:
            b.close()


if __name__ == "__main__":
    main()
