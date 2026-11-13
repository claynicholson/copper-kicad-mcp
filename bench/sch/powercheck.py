"""Power / ground drawing conventions of a bench result, counted from the written .kicad_sch files.

    "<KiCad>/bin/python.exe" bench/sch/powercheck.py [--out DIR] [--json FILE] [design ...]

For every power symbol the wire is followed from the symbol back to the first part pin (or junction / tee) it meets:

  sideways      symbol not upright (ground not pointing down, supply not pointing up)
  wrong_side    ground symbol ABOVE the pin it serves, supply symbol BELOW it
  hooks         more than one bend between pin and symbol, or a path that turns back on itself (a "candy cane")
  top_stubs     ground on a pin that points UP (supply on a pin that points down): stub, sideways, then down to
                the symbol - the two bends the convention asks for there; listed, not counted as hooks
  flags         PWR_FLAG symbols, flags_extra = flags beyond one per wire-connected net on the sheet,
                flags_midwire = a flag closer than 3.81 mm to another power symbol on its own wire

Reads bench/sch/out/<design>/*.kicad_sch (what the last bench run wrote); nothing is modified."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

_env.setup()
from copper_kicad import sexpr  # noqa: E402
from copper_kicad.sch import ops as O  # noqa: E402
from copper_kicad.sch.model import Schematic  # noqa: E402

GND_NAMES = ("GND", "VSS", "AGND", "DGND", "PGND", "EARTH", "GNDA", "GNDD", "GNDPWR")


def _k(p):
    return (round(float(p[0]), 2), round(float(p[1]), 2))


def is_gnd(value: str) -> bool:
    v = value.upper()
    return v.startswith(GND_NAMES) or v in ("0V",)


def check(path: Path) -> dict:
    sch = Schematic.load(path)
    wires = [(_k(w["a"]), _k(w["b"])) for w in sch.wires()]
    juncs = set()
    for j in sch.items("junction"):
        a = sexpr.find(j, "at")
        juncs.add(_k(a[1:3]))
    pin_at, pin_out = {}, {}
    powers, flags = [], []
    for s in sch.symbols():
        pw = s.pins_world()
        if s.is_power:
            if pw:
                (flags if O.is_flag(s) else powers).append((s, _k(pw[0]["xy"]), tuple(int(v) for v in pw[0]["out"])))
        else:
            for p in pw:
                pin_at[_k(p["xy"])] = (s.ref, p["number"])
                pin_out[_k(p["xy"])] = (int(p["out"][0]), int(p["out"][1]))
    ends: dict = {}
    for a, b in wires:
        ends.setdefault(a, []).append(b)
        ends.setdefault(b, []).append(a)

    def inner(pt):
        """wires through whose interior pt lies"""
        return [(a, b) for a, b in wires if pt not in (a, b) and min(a[0], b[0]) - 0.01 <= pt[0] <= max(a[0], b[0]) + 0.01
                and min(a[1], b[1]) - 0.01 <= pt[1] <= max(a[1], b[1]) + 0.01]

    def follow(start):
        """-> (points from the symbol to the first pin / junction / tee / open end, what stopped it)"""
        path, cur, prev = [start], start, None
        while True:
            if cur in pin_at and cur != start:
                return path, "pin"
            if cur in pin_at:
                return path, "pin"
            nxt = [q for q in ends.get(cur, ()) if q != prev]
            if cur in juncs or len(nxt) > 1 or (inner(cur) and cur != start):
                if prev is not None:                      # a tee on the bar of two neighbouring pins: straight on
                    for q in nxt:
                        if q in pin_at and (abs(q[0] - cur[0]) < 0.01) == (abs(prev[0] - cur[0]) < 0.01)                                 and (q[0] - cur[0]) * (cur[0] - prev[0]) >= 0 and (q[1] - cur[1]) * (cur[1] - prev[1]) >= 0:
                            return path + [q], "pin"
                return path, "tee"
            if not nxt:
                return path, "tee" if inner(cur) else "open"
            # a pin tip in the interior of the next segment ends the path there
            a, b = cur, nxt[0]
            mid = [p for p in pin_at if p not in (a, b) and min(a[0], b[0]) - 0.01 <= p[0] <= max(a[0], b[0]) + 0.01
                   and min(a[1], b[1]) - 0.01 <= p[1] <= max(a[1], b[1]) + 0.01]
            if mid:
                mid.sort(key=lambda p: abs(p[0] - a[0]) + abs(p[1] - a[1]))
                return path + [mid[0]], "pin"
            prev, cur = cur, nxt[0]
            path.append(cur)
            if len(path) > 40:
                return path, "loop"

    out = {"powers": len(powers), "sideways": [], "wrong_side": [], "hooks": [], "top_stubs": [], "flags": len(flags),
           "flags_extra": 0, "flags_midwire": []}
    for s, at, o in powers:
        gnd = is_gnd(s.value)
        # the symbol's pin points back INTO the wire: a ground symbol that hangs down has its pin looking up
        upright = (o == (0, -1)) if gnd else (o == (0, 1))
        path, stop = follow(at)
        end = path[-1]
        tag = f"{s.value}@{at[0]},{at[1]}" + (f" <- {pin_at[end][0]}.{pin_at[end][1]}" if end in pin_at else "")
        if not upright:
            out["sideways"].append(tag)
        dirs = []
        for a, b in zip(path, path[1:]):
            d = (0 if abs(a[0] - b[0]) < 0.01 else (1 if b[0] > a[0] else -1),
                 0 if abs(a[1] - b[1]) < 0.01 else (1 if b[1] > a[1] else -1))
            if not dirs or dirs[-1] != d:
                dirs.append(d)
        bends = max(0, len(dirs) - 1)
        back = any(d1[0] == -d2[0] and d1[1] == -d2[1] for i, d1 in enumerate(dirs) for d2 in dirs[i + 1:])
        po = pin_out.get(end) if stop == "pin" else None
        if po is not None and po[0] == 0 and (po[1] < 0) == gnd and bends == 2 and len(dirs) == 3 and dirs[1][1] == 0:
            out["top_stubs"].append(tag)
        elif bends > 1 or back:
            out["hooks"].append(tag + f" bends={bends}")
        if stop == "pin" and end != at and po is not None and po[1] == 0:
            if gnd and at[1] < end[1] - 0.01:
                out["wrong_side"].append(tag + " (ground above its pin)")
            if not gnd and at[1] > end[1] + 0.01:
                out["wrong_side"].append(tag + " (supply below its pin)")
    # flags: one per wire-connected group and net name
    par: dict = {}

    def find(a):
        while par.setdefault(a, a) != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    for a, b in wires:
        par[find(a)] = find(b)
    for j in juncs:
        for a, b in inner(j):
            par[find(j)] = find(a)
    for p in list(pin_at) + [at for _s, at, _o in powers + flags]:
        for a, b in inner(p):
            par[find(p)] = find(a)
    name_of = {}
    for s, at, _o in powers:
        name_of[find(at)] = s.value
    seen: dict = {}
    for s, at, _o in flags:
        key = name_of.get(find(at), find(at))
        seen[key] = seen.get(key, 0) + 1
        near = [q for _s2, q, _o2 in powers if find(q) == find(at) and abs(q[0] - at[0]) + abs(q[1] - at[1]) < 3.82]
        if near:
            out["flags_midwire"].append(f"PWR_FLAG@{at[0]},{at[1]} beside {name_of.get(find(at))}")
    out["flags_extra"] = sum(n - 1 for n in seen.values() if n > 1)
    out["flags_per_net"] = {str(k): n for k, n in seen.items()}
    return out


def main():
    args = sys.argv[1:]
    out_dir = _env.OUT
    js = None
    if "--out" in args:
        i = args.index("--out")
        out_dir = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    if "--json" in args:
        i = args.index("--json")
        js = Path(args[i + 1])
        del args[i:i + 2]
    names = args or sorted([p.name for p in _env.CORPUS.iterdir() if p.is_dir()]
                           + [p.name for p in _env.UNSEEN.iterdir() if p.is_dir()])
    res = {}
    tot = {"sideways": 0, "wrong_side": 0, "hooks": 0, "top_stubs": 0, "flags": 0, "flags_extra": 0, "flags_midwire": 0,
           "powers": 0}
    for name in names:
        d = out_dir / name if (out_dir / name).is_dir() else out_dir / "unseen" / name
        for f in sorted(d.glob("*.kicad_sch")):
            r = check(f)
            res[name] = r
            print(f"{name:20s} powers {r['powers']:3d}  sideways {len(r['sideways'])}  wrong_side {len(r['wrong_side'])}  "
                  f"hooks {len(r['hooks'])}  top_stubs {len(r['top_stubs'])}  flags {r['flags']} "
                  f"(extra {r['flags_extra']}, midwire {len(r['flags_midwire'])})")
            for key in ("sideways", "wrong_side", "hooks", "top_stubs", "flags_midwire"):
                for t in r[key]:
                    print(f"      {key}: {t}")
            for key in tot:
                v = r[key]
                tot[key] += v if isinstance(v, int) else len(v)
    print("TOTAL", tot)
    if js:
        js.write_text(json.dumps({"total": tot, "designs": res}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
