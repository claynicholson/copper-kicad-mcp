"""Which KiCad stock symbol (and footprint) stands for a catalogue part — and the proof that it is the right one.

Owner rule: *use the default (stock) symbols when you can*. Correctness comes first: a stock symbol whose pin numbers
do not match the real part's package pinout is worse than a converted one, so nothing here is accepted on its name.

    select(rec, project_dir, ref)  →  {'choice': {...} | None, 'tried': [...], 'fallback': 'why no stock symbol'}

Candidates, in order:
  1. rule       two-terminal chip parts by kind + package (importer.stock_candidate): Device:R + R_0603_1608Metric.
                Symbol and footprint are both KiCad's, pin 1 is pad 1 by construction; no supplier data needed.
  2. MPN        stock symbols named after the part: exact name, name + packaging suffix (-TR, /NOPB, ',215',
                '(RANGE:…)'), `x` wildcard names (STM32F103C8Tx), family prefixes (LM358DR2G → LM358,
                ESP32-WROOM-32E-N4 → ESP32-WROOM-32E), names that extend the MPN (TP4056 → TP4056-42-ESOP8).
                Derived (`extends`) symbols are ordinary names in the index.
  3. generic    a generic symbol picked from the supplier's own pin data: Transistor_FET:Q_NMOS_GSD /
                Transistor_BJT:Q_NPN_BEC by pin order, Device:Crystal / Crystal_GND24 by which pads are the case,
                Connector_Generic:Conn_01xNN / Conn_02xNN_<scheme> by pad count and numbering, Device:L / D / LED /
                C_Polarized for two-pad parts in non-chip packages (polarity from the supplier's pin names).

Verification of 2 and 3 (`evaluate`), against the supplier's pin data (`ref`: {'pins': {number: name}, 'units',
'pads': pads_of(its footprint), 'pad_names', 'package'} — the importer builds it from the supplier drawing):
  * numbers   the symbol's pin numbers (stacked '[1,15,38,39]' expanded) equal the footprint's pad numbers exactly;
  * footprint a stock footprint is used only when its pads lie on the supplier's pads (≥ 50 % overlap on both axes,
              best of 4 rotations), with the same pad pitch, the same pad type (SMD / through-hole) and the same
              number on every pad — a differently numbered pad must be one the supplier itself calls the same net
              (SOT-223 tab) or a compound pad name that contains the number ('A1B12' ⊇ A1). Otherwise the converted
              footprint is used with the stock symbol, which needs symbol pins == supplier pads;
  * names     pin by pin: a ground on a supply pin (or the reverse) always rejects, so does a name that sits on
              ANOTHER pin of the stock symbol (permuted pinout: TO-92 vs SOT-23, AP7361C-33E vs -33ER, TL431 variants);
              family matches need at least half of the pins positively confirmed, and a family symbol that names
              a footprint must fit on that footprint; an exact MPN name may pass with unnamed supplier pins only on
              the footprint the symbol itself names (the symbol was then drawn for this package);
  * types     a stock output on a supplier input name (or the reverse) is a difference; a stock no-connect on a named
              supplier pin is tolerated (the stock symbol only asks to leave it open);
  * units     multi-unit symbols: every unit's pins must be one channel of the supplier's naming (1IN+/1IN-/1OUT), a
              supplier drawing with N units needs a stock symbol with N (or N + a power unit).

When the supplier drawing cannot be read (offline, no drawing), only an exact MPN name with a default footprint that
names the catalogue package is accepted, and the result says the pinout was not checked against supplier data.
"""
from __future__ import annotations

import bisect
import fnmatch
import functools
import html
import math
import os
import re
from pathlib import Path

from .. import sexpr
from ..kicad import env
from ..mcp.registry import ToolError
from . import common as C
from . import importer as I

# ══ pin names ════════════════════════════════════════════════════════════
_NC = {"NC", "N/C", "N.C.", "N.C", "NC.", "DNC", "NOCONNECT", "NO CONNECT", "NU"}
_CLASSES = {
    "GND": {"GND", "VSS", "AGND", "DGND", "PGND", "SGND", "GNDA", "GNDD", "VSSA", "VEE", "V-", "VCC-", "VSS-", "VEE-", "GROUND"},
    "SUP": {"VCC", "VDD", "VIN", "VI", "IN", "V+", "VS", "VCC+", "VDD+", "VS+", "3V3", "+3V3", "VDD33", "INPUT"},
    "OUT": {"VO", "VOUT", "OUT", "OUTPUT"},
    "ADJ": {"ADJ", "ADJUST"},
    "FB": {"FB", "VFB", "FEEDBACK"},
    "EN": {"EN", "ENA", "ENABLE", "CE", "CHIPEN", "CHIPPU"},
    "RST": {"RST", "RESET", "NRST", "NRESET", "MCLR", "RSTN", "RESETN", "RSTB", "RESETB"},
    "CS": {"CS", "SS", "NSS", "CSN", "NCS", "CSB"},
    "MISO": {"DO", "SO", "MISO", "SDO", "DOUT", "IO1"},
    "MOSI": {"DI", "SI", "MOSI", "SDI", "DIN", "IO0"},
    "CLK": {"CLK", "SCK", "SCLK", "CK"},
    "DP": {"DP", "D+", "UD+", "USBDP", "USBD+", "DP1", "DP2"},
    "DM": {"DM", "DN", "D-", "UD-", "USBDM", "USBD-", "USBDN", "DN1", "DN2"},
    "XI": {"XI", "XTAL1", "OSCIN", "XIN", "OSC1", "XTALIN", "OSCI"},
    "XO": {"XO", "XTAL2", "OSCOUT", "XOUT", "OSC2", "XTALOUT", "OSCO"},
    "K": {"K", "CATHODE", "CAT", "KATHODE"},
    "A": {"A", "ANODE", "AN"},
    "G": {"G", "GATE"}, "S": {"S", "SOURCE"}, "D": {"D", "DRAIN"},
    "B": {"B", "BASE"}, "E": {"E", "EMITTER"}, "C": {"C", "COLLECTOR"},
    "WP": {"WP", "WC"},
    "SH": {"SHIELD", "SHELL", "EH", "SH", "MH", "SHLD", "CASE", "MP"},
    "EP": {"EP", "EPAD", "PAD", "TAB", "THERMALPAD", "POWERPAD", "GNDPAD", "EXPOSEDPAD"},
}
_CLASS_OF = {t: k for k, ts in _CLASSES.items() for t in ts}
_INDEXED = {"GND", "VSS", "VCC", "VDD", "AGND", "PGND", "DGND", "SH", "EH", "MH", "EP", "SHELL", "SHIELD", "PAD", "MP"}   # GND1 / VDD_2 / SH4
_IN_NAMES = {"A", "B", "IN", "I", "INPUT"}
_OUT_NAMES = {"Y", "OUT", "O", "Q", "OUTPUT"}


def _natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def _clean(name) -> str:
    s = str(name or "").strip().upper()
    s = s.replace("~{", "").replace("_{", "").replace("^{", "").replace("}", "").replace("#", "")     # ~{CS}, V_{IN}
    return s.strip("~").strip()


def tokens(name) -> set[str]:
    """Alternative spellings a pin name stands for: 'PF0-OSC_IN(PF0)' → {PF0, OSCIN}; '1IN+' → {1IN+, IN1+}."""
    s = _clean(name)
    out: set[str] = set()
    for part in re.split(r"[/,()|\[\]\s;:]+", s):
        for q in re.split(r"(?<=[A-Z0-9])-(?=[A-Z])", part):
            t = q.replace("_", "").replace(".", "")
            if not t or t.isdigit():
                continue
            out.add(t)
            m = re.fullmatch(r"([+-])([A-Z0-9]+)", t)                # -VSS / +INA
            if m:
                out.add(m.group(2) + m.group(1))
                out.add(m.group(2))
            m = re.fullmatch(r"(\d)([A-Z]+)([+-]?)", t)               # 1OUT / 1IN- → OUT1 / IN1-
            if m:
                out.add(m.group(2) + m.group(1) + m.group(3))
            m = re.fullmatch(r"N([A-Z]{3,})", t)                     # nSLEEP / nFAULT: active-low spelled with an 'n'
            if m and t not in _CLASS_OF:
                out.add(m.group(1))
            m = re.match(r"((?:GPIO|IO|GP|P[A-Z])\d+)_", q)          # GPIO26_ADC0: the port pin leads
            if m:
                out.add(m.group(1))
    return out


def classes(toks: set[str]) -> set[str]:
    out = set()
    for t in toks:
        c = _CLASS_OF.get(t)
        if c:
            out.add(c)
            continue
        if "PAD" in t:
            out.add("EP")
            continue
        m = re.fullmatch(r"([A-Z]+)\d+", t)
        if m and m.group(1) in _INDEXED:
            out.add(_CLASS_OF[m.group(1)])
    return out


def unnamed(name, num="") -> bool:
    s = _clean(name)
    return not s or s == str(num).upper() or bool(re.fullmatch(r"(P|PIN)?_?\d+", s))


def is_nc(name) -> bool:
    return _clean(name).rstrip(".") in _NC or _clean(name) in _NC


def agree(stock_name, sup_name, num="", stock_type="") -> str:
    """Does the stock pin name stand for the supplier's pin name?
    'same' | 'unknown' (one side says nothing) | 'soft' (NC on one side) | 'differs' | 'clash' (ground vs supply)."""
    a, b = _clean(stock_name), _clean(sup_name)
    if unnamed(b, num):
        return "unknown"
    nc_a, nc_b = is_nc(a) or stock_type == "no_connect", is_nc(b)
    if nc_a and nc_b:
        return "same"
    if nc_a or nc_b:
        return "soft"
    if unnamed(a, num):
        return "unknown"
    if a in ("+", "-"):                                  # op-amp inputs: '+' ↔ 1IN+ / IN2+ / +INA
        if b in ("+", "-"):
            return "same" if a == b else "differs"
        ok = "IN" in b and (b.endswith(a) or b.startswith(a)) and "OUT" not in b and not re.match(r"[+-]?V", b)
        return "same" if ok else "differs"
    ta, tb = tokens(a), tokens(b)
    ca, cb = classes(ta), classes(tb)
    if ta & tb or ca & cb:
        if stock_type == "output" and tb & _IN_NAMES and not ta & tb:
            return "differs"
        return "same"
    if "K" in ca and tb == {"C"}:                       # a diode's cathode abbreviated 'C' (the symbol has no collector)
        return "same"
    if ("GND" in ca and "SUP" in cb) or ("SUP" in ca and "GND" in cb):
        return "clash"
    if ca & {"EP", "SH"} or cb & {"EP", "SH"}:
        return "unknown"
    return "differs"


def _base(name) -> str:
    """Channel-less function letter(s) of a logic/analog pin name: '1A' → A, 'OUT2' → OUT, '3Y' → Y."""
    s = _clean(name).replace("_", "")
    m = re.fullmatch(r"\d?([A-Z]+?)\d?[+-]?", s)
    return m.group(1) if m else s


def channel(name) -> str | None:
    """Channel index a supplier pin name carries: 1A / 1IN- / OUT1 / IN2+ / OUTA / +INB → '1' / '2'."""
    s = _clean(name).replace("_", "")
    if "/" in s or not s:
        return None
    m = re.fullmatch(r"(\d)[A-Z]+[+-]?", s) or re.fullmatch(r"[+-]?[A-Z]+?(\d)[+-]?", s)
    if m:
        return m.group(1)
    m = re.fullmatch(r"[+-]?(?:IN|OUT)([A-H])[+-]?", s)
    if m:
        return str(ord(m.group(1)) - 64)
    return None


def compare_names(sym: dict, sup_pins: dict, num_map: dict | None = None) -> dict:
    """Pin-by-pin comparison of a stock symbol with the supplier's pin names.
    num_map: stock pin number → supplier pin number (identity when omitted).
    → {'n', 'same', 'unknown', 'soft', 'differs': [...], 'clash': [...], 'permuted': [...], 'type': [...]}"""
    pins, types = sym["pins"], sym.get("types") or {}
    res = {"n": len(pins), "same": 0, "unknown": 0, "soft": 0, "lost": [], "differs": [], "clash": [], "permuted": [], "type": []}
    confirmed = set()
    for n in sorted(pins, key=_natkey):
        m = (num_map or {}).get(n, n)
        if m not in sup_pins:
            res["unknown"] += 1
            continue
        v = agree(pins[n], sup_pins[m], m, types.get(n, ""))
        if v == "same":
            res["same"] += 1
            confirmed.add(n)
        elif v in ("unknown", "soft"):
            res[v] += 1
            if v == "soft" and is_nc(sup_pins[m]):       # the symbol offers a function on a pin the part leaves open
                res["lost"].append(f"{n}: stock '{pins[n]}' vs supplier '{sup_pins[m]}'")
            # direction: a named stock output on a supplier input letter (or the reverse)
            b = _base(sup_pins[m])
            t = types.get(n, "")
            if (t == "output" and b in _IN_NAMES) or (t == "input" and b in _OUT_NAMES):
                res["type"].append(f"{n}: stock pin is an {t}, supplier names it '{sup_pins[m]}'")
        else:
            text = f"{n}: stock '{pins[n]}' vs supplier '{sup_pins[m]}'"
            res["clash" if v == "clash" else "differs"].append(text)
            if any(k != n and agree(pins[k], sup_pins[m], m, types.get(k, "")) == "same" for k in pins):
                res["permuted"].append(text)
    res["confirmed"] = confirmed
    return res


def unit_check(sym: dict, sup_pins: dict, sup_units: int | None) -> dict:
    """Multi-unit symbol vs the supplier's channel naming. → {'ok', 'notes', 'confirmed': set of pin numbers}"""
    units = {u: ps for u, ps in (sym.get("units") or {}).items() if u > 0}
    out = {"ok": True, "notes": [], "confirmed": set(), "units": len(units) or 1}
    if len(units) < 2:
        return out
    if sup_units and sup_units > 1 and len(units) not in (sup_units, sup_units + 1):
        out["ok"] = False
        out["notes"].append(f"unit count differs: stock symbol has {len(units)} units, the supplier drawing {sup_units}")
        return out
    types, names = sym.get("types") or {}, sym["pins"]
    sig = {u: tuple(sorted((types.get(n, ""), names.get(n, "")) for n in ps)) for u, ps in units.items()}
    repeated = {u for u in units if sum(1 for v in sig.values() if v == sig[u]) >= 2}
    seen: dict[str, int] = {}
    good = []
    for u, ps in sorted(units.items()):
        if u not in repeated:
            continue                                    # power unit / one-off unit: nothing to compare
        chans = {n: channel(sup_pins.get(n, "")) for n in ps}
        cs = {c for c in chans.values() if c}
        if len(cs) > 1:
            out["ok"] = False
            out["notes"].append(f"unit {u} (pins {', '.join(ps)}) mixes supplier channels {sorted(cs)}: "
                                + ", ".join(f"{n}='{sup_pins.get(n, '')}'" for n in ps))
            return out
        if len(cs) == 1 and all(chans.values()):
            c = next(iter(cs))
            if c in seen:
                out["ok"] = False
                out["notes"].append(f"units {seen[c]} and {u} are both the supplier's channel {c}")
                return out
            seen[c] = u
            good += ps
    if len(seen) >= 2:
        out["confirmed"] = set(good)
        out["notes"].append(f"{len(seen)} units match the supplier's channels {sorted(seen)}")
    return out


# ══ footprint geometry ═══════════════════════════════════════════════════
def _xy_points(node, out: list) -> list:
    for e in node:
        if isinstance(e, list):
            if e and str(e[0]) in ("xy", "start", "end", "mid", "center") and len(e) >= 3:
                try:
                    out.append((float(e[1]), float(e[2])))
                except (TypeError, ValueError):
                    pass
            else:
                _xy_points(e, out)
    return out


def pads_of(text: str) -> list[dict]:
    """Numbered copper pads of a .kicad_mod text: [{'n', 'type', 'x', 'y', 'hw', 'hh'}] — centre and half sizes of
    the pad's bounding box in footprint coordinates (pad rotation applied; a custom pad's polygon is included, so
    a SOT-89 tab pad is as large as its copper, not as its anchor)."""
    try:
        tree = sexpr.loads(text)
    except ValueError:
        return []
    out = []
    for pad in sexpr.find_all(tree, "pad"):
        if len(pad) < 4:
            continue
        num, typ, shape = str(pad[1]), str(pad[2]), str(pad[3])
        at, size = sexpr.find(pad, "at"), sexpr.find(pad, "size")
        if not num or typ == "np_thru_hole" or at is None or size is None:
            continue
        x, y = float(at[1]), float(at[2])
        rot = math.radians(float(at[3])) if len(at) > 3 else 0.0
        w, h = float(size[1]), float(size[2])
        pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        if shape == "custom":
            prim = sexpr.find(pad, "primitives")
            if prim is not None:
                pts += _xy_points(prim, [])
        c, sn = math.cos(rot), math.sin(rot)
        gx = [px * c + py * sn for px, py in pts]
        gy = [-px * sn + py * c for px, py in pts]
        out.append({"n": num, "type": typ, "x": x + (min(gx) + max(gx)) / 2, "y": y + (min(gy) + max(gy)) / 2,
                    "hw": (max(gx) - min(gx)) / 2, "hh": (max(gy) - min(gy)) / 2})
    return out


@functools.lru_cache(maxsize=512)
def _fp_pads(path: str) -> tuple:
    return tuple(pads_of(Path(path).read_text(encoding="utf-8", errors="replace")))


def footprint_pads(fp_id: str, project_dir=None) -> list[dict] | None:
    lib, _, name = fp_id.partition(":")
    try:
        L = env.find_lib("fp", lib, project_dir)
    except ToolError:
        return None
    f = Path(L.path) / f"{name}.kicad_mod"
    if not f.exists():
        return None
    return list(_fp_pads(str(f)))


def _signal(pads: list[dict]) -> list[dict]:
    """Drop thermal vias: through-hole pads that repeat the number of an SMD pad."""
    return [q for q in pads if not (q["type"] == "thru_hole" and any(o["type"] == "smd" and o["n"] == q["n"] for o in pads))]


def _overlap(a, b) -> float:
    """Smaller of the per-axis overlaps, as a fraction of the smaller pad's size on that axis (≤ 0: apart)."""
    fx = (min(a[0] + a[2], b[0] + b[2]) - max(a[0] - a[2], b[0] - b[2])) / (2 * min(a[2], b[2]) or 1e-9)
    fy = (min(a[1] + a[3], b[1] + b[3]) - max(a[1] - a[3], b[1] - b[3])) / (2 * min(a[3], b[3]) or 1e-9)
    return min(fx, fy)


def _pitch(pts: list[tuple]) -> float | None:
    """Typical pad pitch: the median distance from a pad to its nearest neighbour (two pads: their distance)."""
    uniq = sorted({(round(x, 2), round(y, 2)) for x, y in pts})
    if len(uniq) < 2:
        return None
    nn = sorted(min(math.hypot(a[0] - b[0], a[1] - b[1]) for b in uniq if b is not a) for a in uniq)
    return nn[len(nn) // 2]


_ROTS = ((0, lambda x, y: (x, y)), (90, lambda x, y: (-y, x)), (180, lambda x, y: (-x, -y)), (270, lambda x, y: (y, -x)))
OVERLAP_MIN = 0.5
PITCH_TOL = 0.08


def match_pads(stock: list[dict], sup: list[dict]) -> dict:
    """Lay the stock footprint over the supplier's (best of 4 rotations, two alignments).
    → {'ok', 'reason', 'rotation', 'pairs': [(stock pad number, supplier pad number)], 'uncovered': [supplier numbers]}
    ok = every stock pad overlaps a supplier pad of the same type, with the same pitch. Numbering is the caller's."""
    sp, rp = _signal(stock), _signal(sup)
    if not sp or not rp:
        return {"ok": False, "reason": "no pads to compare", "pairs": [], "uncovered": []}

    def centres(pads, common, anchors):
        # reference points both footprints share: centre of the pad field, centroid of the pads numbered alike,
        # and a few single pads (a land pattern with one odd pad — SOT-89 tab — still lines up on the others)
        xs, ys = [p["x"] for p in pads], [p["y"] for p in pads]
        out = [((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)]
        sel = [p for p in pads if p["n"] in common]
        if sel:
            out.append((sum(p["x"] for p in sel) / len(sel), sum(p["y"] for p in sel) / len(sel)))
        for n in anchors:
            q = next(p for p in pads if p["n"] == n)
            out.append((q["x"], q["y"]))
        return out
    once = lambda pads: {q["n"] for q in pads if sum(1 for o in pads if o["n"] == q["n"]) == 1}      # noqa: E731
    common = {p["n"] for p in sp} & {p["n"] for p in rp}
    single = sorted(once(sp) & once(rp), key=_natkey)
    anchors = single if len(single) <= 6 else [single[i * (len(single) - 1) // 5] for i in range(6)]
    best = None
    for (sx, sy), (rx, ry) in zip(centres(sp, common, anchors), centres(rp, common, anchors)):
        S = [(q["x"] - sx, q["y"] - sy, q["hw"], q["hh"]) for q in sp]
        for rot, f in _ROTS:
            R = []
            for q in rp:
                x, y = f(q["x"] - rx, q["y"] - ry)
                hw, hh = (q["hw"], q["hh"]) if rot in (0, 180) else (q["hh"], q["hw"])
                R.append((x, y, hw, hh))
            pairs, unmatched, misnum, mistype = [], 0, 0, 0
            covered = set()
            for i, a in enumerate(S):
                ov = [(_overlap(a, b), j) for j, b in enumerate(R)]
                hits = [(o, j) for o, j in ov if o >= OVERLAP_MIN]
                if not hits:
                    unmatched += 1
                    continue
                same = [(o, j) for o, j in hits if rp[j]["n"] == sp[i]["n"]]
                covered.update(jj for _, jj in hits)
                pairs += [(i, jj) for _, jj in hits]
                misnum += not same
                mistype += any(rp[jj]["type"] != sp[i]["type"] for _, jj in hits)
            uncovered = [j for j in range(len(rp)) if j not in covered]
            key = (unmatched, misnum, mistype, len(uncovered))
            if best is None or key < best[0]:
                best = (key, rot, pairs, uncovered, S, R)
    (unmatched, misnum, mistype, _), rot, pairs, uncovered, S, R = best
    res = {"ok": True, "reason": "", "rotation": rot,
           "pairs": sorted({(sp[i]["n"], rp[j]["n"]) for i, j in pairs}, key=lambda t: (_natkey(t[0]), _natkey(t[1]))),
           "uncovered": sorted({rp[j]["n"] for j in uncovered} - {rp[j]["n"] for _, j in pairs}, key=_natkey)}
    if unmatched:
        res.update(ok=False, reason=f"{unmatched} of {len(sp)} pads do not lie on a pad of the supplier footprint (another package?)")
        return res
    if mistype:
        res.update(ok=False, reason="mounting differs (SMD vs through-hole pads)")
        return res
    alike = [(i, j) for i, j in pairs if sp[i]["n"] == rp[j]["n"] and sp[i]["n"] in single]      # one pad each, same number
    if len(alike) >= 3:                                 # a tab / exposed pad is drawn differently by everyone: leads only
        area = lambda q: q["hw"] * q["hh"]              # noqa: E731
        med = sorted(area(sp[i]) for i, _ in alike)[len(alike) // 2]
        leads = [(i, j) for i, j in alike if area(sp[i]) <= 2.5 * med and area(rp[j]) <= 2.5 * med * 2]
        alike = leads if len(leads) >= 2 else alike
    p1, p2 = _pitch([(S[i][0], S[i][1]) for i, _ in alike]), _pitch([(R[j][0], R[j][1]) for _, j in alike])
    if p1 and p2 and abs(p1 - p2) > PITCH_TOL * max(p1, p2):
        res.update(ok=False, reason=f"pad pitch differs ({p1:.2f} mm vs the supplier's {p2:.2f} mm)")
    return res


# ══ candidates: symbols named after the MPN ══════════════════════════════
def _heads(mpn: str) -> list[str]:
    """The MPN and its head before an ordering annotation: 'BAT54C,215', 'MMBT3904(RANGE:100-300)', 'S8050 J3Y(…)'."""
    raw = str(mpn or "").strip()
    out = [raw]
    h = re.split(r"[\s(,]", raw, maxsplit=1)[0]
    if h and h not in out:
        out.append(h)
    return [h for h in out if h]


@functools.lru_cache(maxsize=1)
def _wild_names() -> list[tuple[str, str, str]]:
    """[(lib_id, name, alnum of the name without its 'x' wildcards)] for every stock name with a lowercase x in it."""
    out = []
    for lids in I._sym_index()["exact"].values():
        for lid in lids:
            name = lid.split(":", 1)[1]
            if "x" in name and re.search(r"[A-Z0-9]", name):
                out.append((lid, name, I._alnum(name.replace("x", ""))))
    return out


_VENDOR_PREFIX = ("SN", "MC", "LM", "L", "UA", "KA", "NJM", "HEF", "MM", "DM", "NCV", "NCP", "CD")


@functools.lru_cache(maxsize=1)
def _vendor_index() -> list[tuple[str, str]]:
    """Sorted [(stock name without its vendor prefix, lib_id)] for names that start with one."""
    out = []
    for k, lids in I._sym_index()["exact"].items():
        for vp in _VENDOR_PREFIX:
            if k.startswith(vp) and len(k) - len(vp) >= 5:
                out += [(k[len(vp):], lid) for lid in lids]
    return sorted(set(out))


def _subseq(a: str, b: str) -> bool:
    it = iter(b)
    return all(ch in it for ch in a)


def mpn_candidates(mpn: str, limit: int = 12) -> list[dict]:
    """[{'lib_id', 'match': 'exact' | 'family', 'why'}] — longest / most specific names first. Nothing here is
    trusted: every candidate goes through `evaluate`."""
    idx = I._sym_index()
    out: list[dict] = []
    seen = set()

    def add(lid, match, why):
        if lid not in seen:
            seen.add(lid)
            out.append({"lib_id": lid, "match": match, "why": why})
    heads = _heads(mpn)
    if not heads:
        return out
    ups = [re.sub(r"[^A-Z0-9_.+-]", "", h.upper()) for h in heads]
    for h in heads:
        for lid in idx["exact"].get(I._alnum(h), []):
            add(lid, "exact", "KiCad symbol named after the MPN")
    for h in heads:                                     # trailing packaging / grade suffix
        raw = h.upper()
        for cut in range(1, 6):
            if len(raw) - cut < 4:
                break
            if I._SUFFIX_OK.match(raw[-cut:]):
                for lid in idx["exact"].get(I._alnum(raw[:-cut]), []):
                    add(lid, "exact", f"KiCad symbol named after the MPN without its packaging suffix '{raw[-cut:]}'")
    for lid, rx, name in idx["wild"]:
        body = rx[1:-1]
        for up in ups:
            m = re.match(body, up)
            if m and (m.end() == len(up) or I._SUFFIX_OK.match(up[m.end():])):
                add(lid, "exact", f"KiCad family symbol {name} ('x' = any character)")
    key = I._alnum(heads[-1])
    raw_alnum = [(i, ch) for i, ch in enumerate(heads[-1].upper()) if ch.isalnum()]

    def splits_number(cut: int) -> bool:
        """ABS2|10, LM358|5: the name ends inside a run of digits of the MPN → a different part number.
        (BC817|-16 and LM2596S-5|.0 end at a separator, AP2112K-3.3|TRG1 at a letter.)"""
        if cut >= len(raw_alnum) or not raw_alnum[cut - 1][1].isdigit():
            return False
        return raw_alnum[cut][0] == raw_alnum[cut - 1][0] + 1 and raw_alnum[cut][1].isdigit()
    hits = 0
    for cut in range(len(key) - 1, max(3, (len(key) + 1) // 2 - 1), -1):
        lids = [] if splits_number(cut) else idx["exact"].get(key[:cut], [])
        for lid in lids:
            add(lid, "family", f"KiCad symbol named like the start of the MPN ('{key[:cut]}' of '{heads[-1]}')")
        hits += bool(lids)
        if hits >= 3:
            break
    for lid, rx, name in idx["wild"]:
        body = rx[1:-1]
        if len(name) >= 5 and any(re.match(body, up) for up in ups):
            add(lid, "family", f"KiCad family symbol {name} (the MPN continues with an ordering code)")
    if len(key) >= 5:
        ext = [lid for k, lids in idx["exact"].items() if k.startswith(key) and k != key for lid in lids]
        if len(ext) == 1:
            add(ext[0], "family", f"the only KiCad symbol whose name starts with the MPN '{heads[-1]}'")
    for lid, name, lit in _wild_names():
        if len(lit) >= 6 and key[:5] == lit[:5] and _subseq(lit, key) and len(key) - len(lit) <= name.count("x") + 3:
            add(lid, "family", f"KiCad family symbol {name} (ordering letters differ)")
    # the same jellybean part under a vendor prefix: 78L05G-AB3-R ↔ L78L05_SOT89 / MC78L05_SOT89, SN74HC14DR ↔ 74HC14.
    # Only real vendor prefixes: S8050 is NOT an SS8050.
    for vp in _VENDOR_PREFIX:
        if key.startswith(vp) and len(key) - len(vp) >= 5:
            bare = key[len(vp):]
            for cut in range(len(bare), 4, -1):
                lids = idx["exact"].get(bare[:cut], [])
                for lid in lids:
                    add(lid, "family", f"KiCad symbol of the same part without the vendor prefix '{vp}' ('{bare[:cut]}')")
                if lids:
                    break
    for cut in range(len(key), 4, -1):
        core = key[:cut]
        vi = _vendor_index()
        alias, at = [], bisect.bisect_left(vi, (core, ""))
        while at < len(vi) and vi[at][0].startswith(core):
            alias.append(vi[at][1])
            at += 1
        alias = sorted(set(alias))
        if alias:
            for lid in alias[:8]:
                add(lid, "family", f"KiCad symbol of the same part under a vendor prefix ('{core}' in {lid.split(':', 1)[1]})")
            break
    return out[:limit]


# ══ candidates: generic symbols from the real pinout ═════════════════════
_SOT_FP = {"SOT-23": ["SOT-23", "SOT-23-3"], "SOT-323": ["SOT-323_SC-70"], "SC-70": ["SOT-323_SC-70"], "SC-70-3": ["SOT-323_SC-70"],
           "SOT-523": ["SOT-523"], "SOT-723": ["SOT-723"], "SOT-89": ["SOT-89-3"], "SOT-223": ["SOT-223-3_TabPin2"],
           "TO-252": ["TO-252-2"], "SOT-23-5": ["SOT-23-5"], "SOT-23-6": ["SOT-23-6"]}


def _first_symbol(names: list[str], project_dir) -> str | None:
    for lid in names:
        if I.stock_symbol(lid, project_dir) is not None:
            return lid
    return None


def _polarity(name) -> str | None:
    t = tokens(name) | {_clean(name)}
    if t & {"K", "CATHODE", "CAT", "-", "NEG"}:
        return "K"
    if t & {"A", "ANODE", "AN", "+", "POS"}:
        return "A"
    return None


def _transistor(rec: dict, pins: dict) -> tuple[str, str] | None:
    """(family, pin order) of a plain 3-pin transistor: ('NMOS', 'GSD') / ('NPN', 'BEC')."""
    attrs, typed = rec.get("attributes") or {}, rec.get("typed") or {}
    number = str(attrs.get("Number") or "")
    chan = {"N": "n-channel", "P": "p-channel"}.get(str(typed.get("channel") or "").upper(), "")
    text = " ".join([str(rec.get("description") or ""), str(rec.get("subcategory") or ""), str(attrs.get("type") or ""),
                     str(attrs.get("Type") or ""), number, str(attrs.get("Configuration") or ""),
                     str(typed.get("polarity") or ""), chan]).lower()
    if re.search(r"darlington|pre-?bias|digital transistor|jfet|igbt|array|dual|complementary", text) or re.match(r"\s*[2-9]", number):
        return None
    kind = rec.get("kind")
    if kind == "mosfet":
        n, p = bool(re.search(r"\bn[- ]?ch(annel)?\b|\bnmos\b", text)), bool(re.search(r"\bp[- ]?ch(annel)?\b|\bpmos\b", text))
        fam, want = ("NMOS" if n else "PMOS"), {"G": "G", "S": "S", "D": "D"}
    elif kind == "bjt":
        n, p = bool(re.search(r"\bnpn\b", text)), bool(re.search(r"\bpnp\b", text))
        fam, want = ("NPN" if n else "PNP"), {"B": "B", "E": "E", "C": "C"}
    else:
        return None
    if n == p:
        return None
    order = ""
    for num in ("1", "2", "3"):
        c = classes(tokens(pins.get(num, ""))) & set(want)
        if len(c) != 1:
            return None
        order += next(iter(c))
    return (fam, order) if sorted(order) == sorted(want) else None


def _grid(pads: list[dict]) -> dict | None:
    """Pads numbered 1..n on a regular 1- or 2-row grid → {'rows', 'per', 'pitch', 'scheme'}; None otherwise."""
    cent: dict[str, list] = {}
    for p in pads:
        cent.setdefault(p["n"], []).append((p["x"], p["y"]))
    n = len(cent)
    if n < 1 or set(cent) != {str(k) for k in range(1, n + 1)} or any(len(v) != 1 for v in cent.values()):
        return None
    c = {k: (round(v[0][0], 2), round(v[0][1], 2)) for k, v in cent.items()}
    xs, ys = sorted({v[0] for v in c.values()}), sorted({v[1] for v in c.values()})

    def regular(vals):
        d = [b - a for a, b in zip(vals, vals[1:])]
        return (not d) or max(d) - min(d) < 0.06
    if n == 1:
        return {"rows": 1, "per": 1, "pitch": 0.0, "scheme": ""}
    for along, across in ((xs, ys), (ys, xs)):
        ax = 0 if along is xs else 1
        if len(across) == 1 and len(along) == n and regular(along):
            order = sorted(c, key=lambda k: c[k][ax])
            if order in ([str(k) for k in range(1, n + 1)], [str(k) for k in range(n, 0, -1)]):
                return {"rows": 1, "per": n, "pitch": round(along[1] - along[0], 2), "scheme": ""}
        if len(across) == 2 and n % 2 == 0 and len(along) == n // 2 and regular(along):
            per = n // 2
            col = lambda k: c[k][ax]                      # noqa: E731
            row = lambda k: c[k][1 - ax]                  # noqa: E731
            scheme = None
            if per == 1:
                scheme = "Odd_Even"
            elif row("2") != row("1") and col("2") == col("1"):
                if all(col(str(2 * i + 1)) == col(str(2 * i + 2)) and row(str(2 * i + 1)) == row("1") for i in range(per)):
                    scheme = "Odd_Even"
            elif row("2") == row("1"):
                first = all(row(str(k)) == row("1") for k in range(1, per + 1))
                if first and col(str(2 * per)) == col("1"):
                    scheme = "Counter_Clockwise"
                elif first and col(str(per + 1)) == col("1"):
                    scheme = "Top_Bottom"
            if scheme:
                return {"rows": 2, "per": per, "pitch": round(along[1] - along[0], 2) if per > 1 else round(across[1] - across[0], 2),
                        "scheme": scheme}
    return None


def _positions(rec: dict) -> int | None:
    """Contact count the catalogue states: '1x6P', '2x10P', '4P'."""
    text = f"{rec.get('description') or ''} {rec.get('mfr') or ''}"
    m = re.search(r"(?<![\d.])([12])\s*[x*]\s*(\d{1,2})\s*P", text, re.I)
    if m:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"(?<![\d.x*])(\d{1,2})\s*P(?:in)?\b", text, re.I)
    return int(m.group(1)) if m else None


def generic_candidates(rec: dict, ref: dict, project_dir=None) -> list[dict]:
    """Generic stock symbols chosen from the supplier's own pin data.
    [{'lib_id', 'match': 'generic', 'why', 'footprints': [stock ids to try], 'names': 'construction' | 'check'}]"""
    kind = rec.get("kind") or ""
    pins, padn = ref["pins"], ref["pad_names"]
    n = len(pins)
    out: list[dict] = []
    if not n or set(pins) != set(padn) or ref.get("duplicate_pin_numbers"):
        return out                                      # mechanical pads / shared numbers: no generic symbol covers them
    text = f"{rec.get('description') or ''} {rec.get('subcategory') or ''}".lower()
    pk = C.norm_pkg(rec.get("package"))
    sot = [f"Package_TO_SOT_SMD:{f}" for f in _SOT_FP.get(pk, [])]

    def add(lid, why, fps=(), names="construction"):
        if lid:
            out.append({"lib_id": lid, "match": "generic", "why": why, "footprints": list(fps), "names": names})
    if kind in ("mosfet", "bjt") and n == 3:
        t = _transistor(rec, pins)
        if t:
            fam, order = t
            lib = "Transistor_FET" if kind == "mosfet" else "Transistor_BJT"
            add(_first_symbol([f"{lib}:Q_{fam}_{order}", f"Device:Q_{fam}_{order}"], project_dir),
                f"generic {fam} with the part's pin order {order} (supplier pins 1/2/3 = {'/'.join(pins[k] for k in '123')})",
                sot, names="check")
    elif kind == "crystal" and "oscillator" not in (rec.get("subcategory") or "").lower():
        m = re.search(r"(\d{4})-(\d)P", rec.get("package") or "")
        fps = []
        if m:
            fps = _glob_fp("Crystal", f"Crystal_SMD_{m.group(1)}-{m.group(2)}Pin_*")
        elif re.search(r"HC-?49S?-?SMD", rec.get("package") or "", re.I):
            fps = ["Crystal:Crystal_SMD_HC49-SD"]
        if n == 2:
            add("Device:Crystal", "generic 2-pad crystal", fps)
        elif n == 4:
            gnd = {k for k, v in pins.items() if classes(tokens(v)) & {"GND", "SH"}}
            osc = {k for k, v in pins.items() if k not in gnd and not unnamed(v, k) and not is_nc(v)}
            if len(gnd) == 2 and len(osc) == 2:
                name = "Crystal_GND" + "".join(sorted(gnd))
                add(_first_symbol([f"Device:{name}"], project_dir),
                    f"generic 4-pad crystal with the case on pads {' and '.join(sorted(gnd))} (supplier names them "
                    f"{'/'.join(pins[k] for k in sorted(gnd))})", fps)
    elif kind in ("header", "connector"):
        g = _grid(ref["pads"])
        stated = _positions(rec)
        if g and (stated is None or stated == n) and all(unnamed(v, k) for k, v in pins.items()):
            fps = _header_fp(rec, ref, g)
            screw = "screw terminal" in text or "terminal block" in text
            if g["rows"] == 1 and n <= 40:
                lid = _first_symbol(([f"Connector:Screw_Terminal_01x{n:02d}"] if screw else []) +
                                    [f"Connector_Generic:Conn_01x{n:02d}"], project_dir)
                add(lid, f"generic 1x{n} connector: {n} contacts numbered 1..{n} in one row, no mechanical pads", fps)
            elif g["rows"] == 2 and g["per"] <= 40:
                add(_first_symbol([f"Connector_Generic:Conn_02x{g['per']:02d}_{g['scheme']}"], project_dir),
                    f"generic 2x{g['per']} connector: the supplier footprint numbers its pads {g['scheme'].replace('_', '/')}", fps)
    elif kind == "bridge" and n == 4:
        order = ""
        for k in ("1", "2", "3", "4"):
            nm = html.unescape(str(pins.get(k, ""))).strip().upper()
            order += "+" if nm in ("+", "DC+", "V+") else "-" if nm in ("-", "DC-", "V-") else                 "A" if nm in ("~", "AC", "AC1", "AC2", "~1", "~2", "∼") else "?"
        if sorted(order) == sorted("+-AA"):
            add(_first_symbol([f"Device:D_Bridge_{order}"], project_dir),
                f"generic bridge rectifier with the part's pin order {order} (supplier pins 1..4 = "
                f"{'/'.join(html.unescape(pins[k]) for k in '1234')})")
    elif kind == "switch" and n == 2 and re.search(r"tactile|push|button", text):
        add("Switch:SW_Push", "generic push button (two contacts)")
    elif kind == "usb_connector":
        for lid in ("Connector:USB_C_Receptacle_USB2.0_16P", "Connector:USB_C_Receptacle_USB2.0_14P",
                    "Connector:USB_C_Receptacle_PowerOnly_6P", "Connector:USB_C_Receptacle", "Connector:USB_B_Micro",
                    "Connector:USB_B_Mini", "Connector:USB_A", "Connector:USB_B"):
            if I.stock_symbol(lid, project_dir) is not None:
                add(lid, f"KiCad's {lid.split(':', 1)[1]} on KiCad's footprint for exactly this connector", names="check")
                out[-1]["mpn_footprint_only"] = True
    elif n == 2 and pk not in I._CHIP:
        nonpolar = {"inductor": "Device:L", "ferrite": "Device:FerriteBead_Small", "capacitor": "Device:C",
                    "fuse": "Device:Polyfuse" if re.search(r"ptc|resettable", text) else "Device:Fuse"}
        if kind == "resistor" and not re.search(r"network|array|potentiometer|trimmer|varistor|thermistor|ntc|ptc|photo", text):
            nonpolar["resistor"] = "Device:R"
        pol = {k: _polarity(v) for k, v in pins.items()}
        if kind in nonpolar:
            add(nonpolar[kind], f"generic two-terminal {kind} (no polarity)")
        elif kind in ("diode", "schottky", "zener", "led") and pol.get("1") == "K" and pol.get("2") == "A":
            if not (kind == "led" and re.search(r"rgb|bi-?color|dual", text)) and \
                    not (kind != "led" and re.search(r"\b(dual|series|common|array)\b", text)):
                lid = {"diode": "Device:D", "schottky": "Device:D_Schottky", "zener": "Device:D_Zener", "led": "Device:LED"}[kind]
                add(lid, f"generic {kind}: the supplier's pad 1 is the cathode, as on {lid}")
        elif kind in ("tvs", "esd"):
            uni = I.tvs_unidirectional(rec)
            if uni is False:
                add("Device:D_TVS", "generic bidirectional TVS (no polarity)")
            elif uni and pol.get("1") == "K" and pol.get("2") == "A":
                add("Device:D_Zener", "unidirectional TVS drawn as Device:D_Zener: the supplier's pad 1 is the cathode")
        elif kind in ("electrolytic", "tantalum") and pol.get("1") == "A" and pol.get("2") == "K":
            add("Device:C_Polarized", "generic polarised capacitor: the supplier's pad 1 is the positive terminal")
    return out


@functools.lru_cache(maxsize=256)
def _glob_fp(lib: str, pattern: str) -> list[str]:
    try:
        L = env.find_lib("fp", lib)
    except ToolError:
        return []
    return [f"{lib}:{n}" for n in sorted(f.stem for f in Path(L.path).glob(pattern + ".kicad_mod"))
            if not re.search(r"hand-?solder", n, re.I)]


def _header_fp(rec: dict, ref: dict, g: dict) -> list[str]:
    """KiCad's PinHeader / PinSocket footprint for a plain vertical through-hole header (geometry is checked later)."""
    sub = (rec.get("subcategory") or "").lower()
    title = ref.get("package") or ""
    if "pin headers" not in sub and "female headers" not in sub:
        return []
    if not re.search(r"-V(-|$|_)", title) or re.search(r"shroud|box|idc|ejector", (rec.get("description") or "").lower()):
        return []
    p = f"{g['pitch']:.2f}"
    if p not in ("2.54", "2.00", "1.27", "1.00") or (g["rows"] == 2 and g["scheme"] != "Odd_Even"):
        return []
    fam = "PinSocket" if "female" in sub else "PinHeader"
    return [f"Connector_{fam}_{p}mm:{fam}_{g['rows']}x{g['per']:02d}_P{p}mm_Vertical"]


# ══ footprints for a stock symbol ════════════════════════════════════════
@functools.lru_cache(maxsize=1)
def _fp_index() -> list:
    """[[lib, name, alnum(name)]] for every footprint of the global libraries (cached per KiCad version)."""
    try:
        inst = env.kicad()
        ver = inst.full_version or inst.version
    except ToolError:
        return []
    hit = C.cache_get("fpidx", f"v1-{ver}", ttl=30 * 86400)
    if hit:
        return hit
    out = []
    for lib in env.lib_tables("fp"):
        if lib.scope == "project" or not lib.exists:
            continue
        try:
            for f in os.scandir(lib.path):
                if f.name.endswith(".kicad_mod"):
                    out.append([lib.name, f.name[:-10], I._alnum(f.name[:-10])])
        except OSError:
            pass
    C.cache_put("fpidx", f"v1-{ver}", out)
    return out


def footprint_candidates(cand: dict, sym: dict, rec: dict, limit: int = 40) -> list[tuple[str, str, bool]]:
    """Stock footprints worth checking for this symbol: [(fp_id, how it was found, drawn_for)].
    drawn_for = the symbol (or KiCad's footprint for this very MPN) names it, so a geometric fit also confirms that
    the symbol was drawn for this package; a footprint found by filter or package name confirms nothing about pins."""
    out: list[tuple[str, str, bool]] = []
    seen = set()

    def add(fid, how, drawn_for=True):
        if fid and fid not in seen:
            seen.add(fid)
            out.append((fid, how, drawn_for))
    npins = len(sym["pins"])
    if not cand.get("mpn_footprint_only"):
        add(sym.get("footprint"), "the symbol's default footprint")
        for fid in cand.get("footprints") or []:
            add(fid, "KiCad's footprint for this package")
    for h in _heads(rec.get("mfr") or ""):
        key = I._alnum(h)
        if len(key) >= 6:
            for lib, name, an in sorted((r for r in _fp_index() if key in r[2]), key=lambda r: len(r[1]))[:3]:
                add(f"{lib}:{name}", "KiCad footprint named after the MPN")
    if cand["match"] == "generic" or (cand["match"] == "family" and sym.get("footprint")):
        return out          # generic symbols: only footprints named for the package / part; a family symbol drawn
                            # for one package is not moved to another
    pats = [p for p in sym.get("fp_filters") or [] if p.strip("*?")]
    # the catalogue package as KiCad names it: SOIC-14 → SOIC-14_3.9x8.7mm_P1.27mm, LQFP-48 → LQFP-48_7x7mm_P0.5mm
    pkg = C.norm_pkg(rec.get("package"))
    if re.fullmatch(r"[A-Z]+-\d+W?", pkg):
        base = pkg.rstrip("W")
        pats += [f"{base}_*", f"{base}-*", f"{base}W_*"]
        if base.startswith(("LQFN", "WQFN", "VQFN", "UQFN", "UFQFPN", "TQFN")):
            pats += [f"QFN-{base.split('-')[1]}-*", f"QFN-{base.split('-')[1]}_*"]
    if pats:
        hits = []
        for lib, name, _ in _fp_index():
            if re.search(r"hand-?solder", name, re.I):
                continue
            if any(fnmatch.fnmatchcase(name.lower(), p.lower()) or
                   (":" in p and fnmatch.fnmatchcase(f"{lib}:{name}".lower(), p.lower())) for p in pats):
                hits.append((len(name), f"{lib}:{name}"))
        pk = I._alnum(C.norm_pkg(rec.get("package")))
        hits.sort(key=lambda t: (0 if pk and pk in I._alnum(t[1]) else 1, t))
        budget = limit
        for _, fid in hits:
            if budget <= 0:
                break
            pads = I.footprint_pads(fid)
            budget -= 1
            if pads is not None and len(pads) == npins:
                add(fid, "a KiCad footprint for this package (symbol filter / package name)", False)
    return out


def _pkg_names_fp(pkg: str, fp: str) -> bool:
    """Loose name test, used only when there is no supplier drawing: does the stock footprint's name mention the
    catalogue package (SOT-23 ↔ SOT-23, SOP-8 ↔ SOIC-8, LQFP-48 ↔ LQFP-48)?"""
    name = I._alnum(fp.split(":", 1)[-1])
    t = re.split(r"[(,]", str(pkg or ""))[0].strip().upper()
    t = re.sub(r"-(\d+)L$", r"-\1", t)
    tok = re.sub(r"[^A-Z0-9]", "", t)
    if not tok:
        return False
    if C.norm_pkg(pkg) in C.CHIP_SIZES:
        return C.norm_pkg(pkg) in fp
    alts = {tok, tok.replace("SOP", "SOIC"), tok.replace("SOIC", "SO"), tok.replace("ESOP", "SOIC"), tok.replace("SOT233", "SOT23"),
            tok.replace("LQFN", "QFN"), tok.replace("UFQFPN", "QFN"), tok.replace("WQFN", "QFN"), tok.replace("VQFN", "QFN"),
            tok.replace("TQFP", "QFP"), tok.replace("LQFP", "QFP"), tok.replace("SOT2233", "SOT223"), tok.replace("SOT893", "SOT89"),
            tok.replace("HTSSOP", "TSSOP"), tok.replace("SC70", "SOT323")}
    return any(a and a in name for a in alts)


# ══ verification ═════════════════════════════════════════════════════════
def _need(n: int) -> int:
    return min(n, max(2, math.ceil(n / 2)))


def _names_verdict(cand: dict, sym: dict, ref: dict, num_map: dict | None, stock_fp_ok: bool) -> tuple[bool, list[str]]:
    """Is the stock symbol's pinout the supplier's? → (ok, notes)"""
    n = len(sym["pins"])
    cmp_ = compare_names(sym, ref["pins"], num_map)
    notes = []
    if cmp_["clash"]:
        return False, ["ground/supply on different pins than the supplier drawing: " + "; ".join(cmp_["clash"][:4])]
    if cmp_["permuted"]:
        return False, ["pinout is permuted vs the supplier drawing: " + "; ".join(cmp_["permuted"][:4])]
    if cmp_["type"]:
        return False, ["pin direction differs: " + "; ".join(cmp_["type"][:4])]
    if len(cmp_["lost"]) > max(1, n // 12) or cmp_["soft"] > max(1, n // 4):
        return False, [f"{cmp_['soft']} pins are no-connect on one side only (another variant of the part?): "
                       + "; ".join(cmp_["lost"][:4])]
    uc = unit_check(sym, ref["pins"], ref.get("units"))
    if not uc["ok"]:
        return False, uc["notes"]
    notes += uc["notes"]
    if cand.get("names") == "construction":
        return True, notes
    exact = cand["match"] == "exact"
    allowed = (1 if n >= 6 else 0) if n < 24 else n // 12
    if not exact:
        allowed = n // 16
    if len(cmp_["differs"]) > allowed:
        return False, [f"{len(cmp_['differs'])} pin name(s) differ from the supplier drawing: " + "; ".join(cmp_["differs"][:4])]
    confirmed = len(cmp_["confirmed"] | uc["confirmed"])
    if cand["match"] == "generic":
        if confirmed < n:
            return False, [f"only {confirmed} of {n} pin names confirmed by the supplier drawing"]
    elif confirmed < _need(n) and not (exact and stock_fp_ok and not cmp_["differs"]):
        if exact:
            return False, [f"only {confirmed} of {n} pin names confirmed by the supplier drawing, and no stock footprint "
                           f"confirms the package"]
        return False, [f"only {confirmed} of {n} pin names confirmed by the supplier drawing (a family match needs {_need(n)})"]
    if cmp_["differs"]:
        notes.append("tolerated name difference: " + "; ".join(cmp_["differs"]))
    notes.append(f"{confirmed} of {n} pins confirmed by name" + (f", {cmp_['soft']} NC" if cmp_["soft"] else ""))
    return True, notes


def _numbering(fp_pads: list[dict], sym: dict, ref: dict) -> tuple[bool, list[str], dict]:
    """Stock footprint vs supplier footprint: geometry, then the number on every pad. → (ok, notes, num_map)"""
    g = match_pads(fp_pads, ref["pads"])
    if not g["ok"]:
        return False, [g["reason"]], {}
    sup = ref["pins"]
    num_map: dict[str, str] = {}
    notes = []
    for a, b in g["pairs"]:
        if a == b:
            num_map[a] = b
            continue
        if a in re.findall(r"[A-Z]+\d+", b) and b not in sym["pins"]:
            num_map[a] = b                              # compound supplier pad 'A1B12' carries stock pads A1 and B12
            continue
        sa, sb = sym["pins"].get(a, ""), sup.get(b, "")
        ca, cb = classes(tokens(sa)), classes(tokens(sb))
        if a in sup and agree(sup[a], sb, b) == "same":
            notes.append(f"pad {a} also covers the supplier's pad {b} (both '{sb or sup[a]}')")
            continue                                    # the supplier calls both pads the same net (SOT-223 tab)
        if ca & {"SH", "EP"} and (cb & {"SH", "EP", "GND"} or b not in sup):
            num_map.setdefault(a, b)                    # shield / exposed pad under another number
            continue
        return False, [f"pad numbering differs: stock pad {a} ('{sa}') lies on the supplier's pad {b} ('{sb}')"], {}
    covered = {b for _, b in g["pairs"]}
    for b in g["uncovered"]:
        nm = sup.get(b)
        if b in covered:
            continue
        if nm is None or is_nc(nm) or classes(tokens(nm)) & {"EP", "SH"}:
            notes.append(f"supplier pad {b} ('{nm or 'mechanical, no pin'}') has no pad in the stock footprint")
            continue
        return False, [f"the supplier's pad {b} ('{nm}') has no pad in the stock footprint"], {}
    return True, notes, num_map


def evaluate(cand: dict, rec: dict, ref: dict | None, project_dir=None) -> dict:
    """Check one candidate. → {'ok', 'lib_id', 'footprint' (None = use the converted one), 'footprint_source',
    'notes': [...], 'pins', 'units'}"""
    lid = cand["lib_id"]
    res = {"ok": False, "lib_id": lid, "footprint": None, "footprint_source": None, "match": cand["match"],
           "why": cand["why"], "notes": []}
    sym = I.stock_symbol(lid, project_dir)
    if sym is None:
        res["notes"].append("symbol not found in the stock libraries")
        return res
    res["pins"], res["units"] = sym["pins"], sym["unit_count"]
    if not sym["pins"]:
        res["notes"].append("symbol has no pins")
        return res
    if ref is None:
        # no supplier drawing: exact MPN name + its own default footprint that names the catalogue package
        fp = sym.get("footprint") or ""
        ok, notes, _ = I.verify_stock(lid, fp, rec, project_dir)
        if cand["match"] != "exact":
            res["notes"].append("a family / generic match cannot be accepted without the supplier's pin data")
            return res
        if not ok:
            res["notes"] += notes
            return res
        if not _pkg_names_fp(rec.get("package") or "", fp):
            res["notes"].append(f"default footprint {fp} does not name the catalogue package '{rec.get('package')}' "
                                f"and there is no supplier drawing to check the pinout against")
            return res
        res.update(ok=True, footprint=fp, footprint_source="stock", verified="package name only (no supplier drawing)")
        res["notes"].append("pin numbers cover the footprint pads exactly; pinout NOT checked against supplier data (unavailable)")
        return res
    rejected = []
    for fid, how, drawn_for in footprint_candidates(cand, sym, rec):
        ok, notes, _ = I.verify_stock(lid, fid, rec, project_dir)
        if not ok:
            rejected.append(f"{fid}: {'; '.join(notes)}")
            continue
        pads = footprint_pads(fid, project_dir)
        ok, notes, num_map = _numbering(pads or [], sym, ref)
        if not ok:
            rejected.append(f"{fid}: {'; '.join(notes)}")
            continue
        ok2, nnotes = _names_verdict(cand, sym, ref, num_map, drawn_for)
        if not ok2:
            res["notes"] += nnotes
            res["rejected_footprints"] = rejected
            return res
        res.update(ok=True, footprint=fid, footprint_source="stock", verified="supplier pin data + pad geometry")
        res["notes"] += [f"footprint: {how}; its pads lie on the supplier's pads with the same numbering"] + notes + nnotes
        return res
    res["rejected_footprints"] = rejected
    if cand.get("mpn_footprint_only"):
        res["notes"].append("KiCad ships no footprint for exactly this part that this symbol's pins cover" +
                            (f" ({'; '.join(rejected)[:240]})" if rejected else ""))
        return res
    if cand["match"] == "family" and sym.get("footprint"):
        res["notes"].append(f"the symbol is drawn for {sym['footprint']}, which does not fit this part"
                            + (f" ({'; '.join(rejected)[:240]})" if rejected else ""))
        return res
    # stock symbol + converted footprint: pin numbers must be the supplier's pad numbers, exactly
    spins, spads = set(sym["pins"]), set(ref["pad_names"])
    if spins != spads:
        miss, extra = sorted(spads - spins, key=_natkey), sorted(spins - spads, key=_natkey)
        res["notes"].append("pin numbers do not cover the part's pads exactly: "
                            + "; ".join(x for x in (f"pads {miss} have no pin" if miss else "", f"pins {extra} have no pad" if extra else "") if x)
                            + ("" if not rejected else f" (stock footprints tried: {'; '.join(rejected)[:300]})"))
        return res
    ok2, nnotes = _names_verdict(cand, sym, ref, None, False)
    if not ok2:
        res["notes"] += nnotes
        return res
    res.update(ok=True, footprint=None, footprint_source="converted", verified="supplier pin data")
    res["notes"] += ["footprint: converted from the supplier drawing (" +
                     ("no stock footprint fits: " + "; ".join(rejected)[:240] if rejected else "the symbol names no footprint for this package")
                     + "); symbol pin numbers equal its pad numbers"] + nnotes
    return res


# ══ selection ════════════════════════════════════════════════════════════
def select(rec: dict, project_dir=None, ref_loader=None) -> dict:
    """→ {'choice': evaluate() result | None, 'tried': [{'lib_id','footprint','ok','notes'}], 'fallback': str,
    'reference': 'supplier drawing' | 'unavailable: …' | 'not needed'}"""
    tried: list[dict] = []
    out = {"choice": None, "tried": tried, "fallback": "", "reference": "not needed"}
    sc = I.stock_candidate(rec)
    if sc:
        ok, notes, sym = I.verify_stock(sc["lib_id"], sc["footprint"], rec, project_dir)
        tried.append({"lib_id": sc["lib_id"], "footprint": sc["footprint"], "ok": ok, "notes": notes})
        if ok:
            out["choice"] = {"ok": True, "lib_id": sc["lib_id"], "footprint": sc["footprint"], "footprint_source": "stock",
                             "match": "rule", "why": sc["why"], "pins": sym.get("pins", {}), "units": sym.get("unit_count", 1),
                             "verified": "by construction (KiCad symbol + KiCad footprint for this package)",
                             "notes": ["two-terminal part: KiCad symbol and KiCad footprint for the package; pin numbers "
                                       "cover the footprint pads exactly"]}
            return out
    mpn = rec.get("mfr") or ""
    cands = mpn_candidates(mpn)
    ref, why_no_ref = None, ""
    if ref_loader is not None:
        try:
            ref = ref_loader()
            if ref is None:
                why_no_ref = "the supplier drawing could not be read"
        except Exception as e:  # noqa: BLE001 — offline / no drawing / unreadable drawing: all mean 'no reference'
            why_no_ref = str(e)[:160]
    else:
        why_no_ref = "not requested"
    out["reference"] = "supplier drawing" if ref is not None else f"unavailable: {why_no_ref}"
    if ref is not None:
        cands += generic_candidates(rec, ref, project_dir)
    for cand in cands:
        r = evaluate(cand, rec, ref, project_dir)
        tried.append({"lib_id": r["lib_id"], "footprint": r.get("footprint") or "", "ok": r["ok"], "match": cand["match"],
                      "notes": r["notes"] + (r.get("rejected_footprints") or [])[:2]})
        if r["ok"]:
            out["choice"] = r
            return out
    if not tried:
        out["fallback"] = ("no KiCad stock symbol is named after the MPN" + (f" '{mpn}'" if mpn else "") +
                           " and no generic symbol fits the part's kind and pinout")
    else:
        bad = [t for t in tried if not t["ok"]]
        out["fallback"] = "; ".join(f"{t['lib_id']} rejected ({'; '.join(t['notes'])[:220] or 'no fit'})" for t in bad[:3])
        if len(bad) > 3:
            out["fallback"] += f"; and {len(bad) - 3} more"
    if ref is None and ref_loader is not None:
        out["fallback"] += f" [supplier drawing unavailable: {why_no_ref}]"
    return out
