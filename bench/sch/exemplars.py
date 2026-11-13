"""Measure human-drawn reference sheets (the KiCad demo projects shipped with the local install, read-only) and
print the constants that go into src/copper_kicad/sch/pro/exemplar.py.

    "<KiCad>/bin/python.exe" bench/sch/exemplars.py [demos dir]

Nothing is read at run time by the package: the numbers are checked in. Only geometry that needs no netlist is
measured (labels per part, nearest-neighbour part distance, sheet fill, PWR_FLAGs, notes, power-symbol direction).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

_env.setup()
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.sch import layout as LAY  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.model import Schematic  # noqa: E402


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    i = q * (len(xs) - 1)
    lo, hi = int(i), min(len(xs) - 1, int(i) + 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (i - lo), 3)


def measure(f: Path):
    sch = Schematic.load(f, f.stem, None)
    parts = {}
    for s in sch.symbols(include_power=False):
        parts.setdefault(s.ref, s.at)
    if len(parts) < 8:
        return None
    pts = list(parts.values())
    nn = []
    for i, a in enumerate(pts):
        nn.append(min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for j, b in enumerate(pts) if j != i))
    w, h = sch.page_size()
    boxes = [s.body_bbox() for s in sch.symbols()]
    used = (max(b[2] for b in boxes) - min(b[0] for b in boxes)) * (max(b[3] for b in boxes) - min(b[1] for b in boxes))
    pw = [s for s in sch.symbols() if s.is_power and not O.is_flag(s)]
    up = 0
    for s in pw:
        pins = s.pins_world()
        bb = s.body_bbox()
        if pins:
            dy = (bb[1] + bb[3]) / 2 - pins[0]["xy"][1]
            dx = (bb[0] + bb[2]) / 2 - pins[0]["xy"][0]
            if abs(dy) >= abs(dx) and ((dy > 0) == LAY.is_ground(s.value)):
                up += 1
    return {"parts": len(parts), "labels_per_part": len(sch.labels()) / len(parts), "nn_mm": pct(nn, 0.5),
            "fill": used / max(1.0, (w - 25) * (h - 25)), "flags": sum(1 for s in sch.symbols() if O.is_flag(s)),
            "notes": len(sch.items("text")), "wires_per_part": len(sch.wires()) / len(parts),
            "power_upright": up / len(pw) if pw else None}


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(env.kicad().cli).parent.parent / "share" / "kicad" / "demos"
    rows = []
    for f in sorted(root.rglob("*.kicad_sch")):
        try:
            m = measure(f)
        except Exception as e:  # noqa: BLE001
            print(f"skip {f.name}: {type(e).__name__}", file=sys.stderr)
            continue
        if m:
            rows.append(m)
    out = {"sheets": len(rows)}
    for key in ("labels_per_part", "nn_mm", "fill", "notes", "wires_per_part", "power_upright"):
        xs = [r[key] for r in rows if r[key] is not None]
        out[key] = {"p25": pct(xs, 0.25), "p50": pct(xs, 0.5), "p75": pct(xs, 0.75)}
    out["flags"] = {"p50": pct([r["flags"] for r in rows], 0.5), "p90": pct([r["flags"] for r in rows], 0.9)}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
