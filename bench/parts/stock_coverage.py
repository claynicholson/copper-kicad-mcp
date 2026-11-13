"""Stock-symbol coverage bench: for each part in corpus.CORPUS, does the importer pick a KiCad stock symbol or a
converted (EasyEDA) one, why was a stock one rejected or not found, and is a chosen stock symbol actually RIGHT?

    "C:/Program Files/KiCad/10.0/bin/python.exe" bench/parts/stock_coverage.py            # cache first, network for misses
    ... stock_coverage.py --offline        # cache only (COPPER_OFFLINE=1); parts never fetched are reported as such
    ... stock_coverage.py --refresh        # honour the engine's normal cache TTLs (re-queries stale entries)
    ... stock_coverage.py --only C20917,C5446   # a subset
    ... stock_coverage.py --json out.json  # where the machine-readable result goes (default bench/parts/out/…)
    ... stock_coverage.py --table          # the whole bundled offline table through the selection, cache only: parts
                                           # whose supplier drawing was never fetched can only match by rule / exact name
    ... stock_coverage.py --table --online # same, fetching the supplier drawing of every non-rule part (C-numbers only)

What is measured (the importer itself is called, nothing is re-implemented):
  * chosen     importer.resolve_lcsc(scratch project, C-number, with_3d=False) — the exact call sch_apply's
               place op makes for {"op":"place","lcsc":…} and, after basic.pick, for {"op":"place","spec":…}.
  * truth      the supplier's own drawing for that C-number (EasyEDA symbol pins + footprint pads), used as the
               reference pinout. It is a reference, not gospel: disagreements are printed for a human to judge.
  * audit      a chosen stock symbol+footprint is compared with the reference: pin-number set, pin names per
               number, SMD/through-hole, pad count, pad positions per number (best of 4 rotations).
  * missed     for a converted part: which stock symbols WOULD have matched the reference pinout (the MPN's
               family symbol the name lookup did not reach, or a generic Device:/Connector: symbol), and whether
               the stock footprint also fits or only the symbol does.

Everything (Copper home, part cache, KiCad config, the scratch project) lives under bench/parts/; only C-numbers
are sent anywhere, to the same JLC / EasyEDA endpoints the importer uses.
"""
from __future__ import annotations

import argparse
import collections
import itertools
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CACHE = HERE / "cache"            # COPPER_HOME: part cache (kept, so the bench re-runs offline)
WORK = HERE / ".work"             # scratch project + KiCad config (disposable)
OUT = HERE / "out"


def setup(offline: bool, refresh: bool):
    for d in (CACHE, WORK / "kicad-config", WORK / "tmp", OUT):
        d.mkdir(parents=True, exist_ok=True)
    os.environ["COPPER_HOME"] = str(CACHE)
    os.environ["KICAD_CONFIG_HOME"] = str(WORK / "kicad-config")
    for k in ("TEMP", "TMP", "TMPDIR"):
        os.environ[k] = str(WORK / "tmp")
    if offline:
        os.environ["COPPER_OFFLINE"] = "1"
    else:
        os.environ.pop("COPPER_OFFLINE", None)
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(HERE))
    from copper_kicad.parts import common as C
    C.MIN_INTERVAL.setdefault("easyeda.com", 0.5)      # the bench asks for many drawings in a row: stay low-rate
    if not refresh:
        # the bench is cache-first: an entry fetched once is good forever (the engine's own TTL is 1 h for JLC)
        orig = C.cache_get
        C.cache_get = lambda ns, key, ttl: orig(ns, key, None)


# ── reference drawing (EasyEDA) ──────────────────────────────────────────
_PAD_RX = re.compile(r'\(pad\s+"([^"]*)"\s+(\w+)\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)\s*\(size\s+([-\d.]+)\s+([-\d.]+)\)')


def pads_of(text: str) -> list[dict]:
    out = []
    for m in _PAD_RX.finditer(text):
        num, typ = m.group(1), m.group(2)
        if not num or typ == "np_thru_hole":
            continue
        out.append({"n": num, "type": typ, "x": float(m.group(3)), "y": float(m.group(4)),
                    "w": float(m.group(6)), "h": float(m.group(7))})
    return out


def reference(code: str, kind: str) -> dict:
    """{'pins': {num: name}, 'pads': [...], 'th': bool} from the supplier drawing, or {'error': …}."""
    from copper_kicad.parts import common as C, easyeda as E
    try:
        comp = E.fetch(code)
    except (C.NetError, E.ConvertError) as e:
        return {"error": f"no supplier drawing: {e}"}
    out: dict = {}
    try:
        _, sinfo = E.build_symbol(comp, "REF", {"Value": "REF"}, footprint="", kind=kind or "")
        out["pins"] = {str(k): str(v or "") for k, v in sinfo["pins"].items()}
        out["units"] = sinfo.get("units")
    except Exception as e:  # noqa: BLE001 — a bench must report, not die
        out["sym_error"] = f"{type(e).__name__}: {e}"
    try:
        text, meta = E.build_footprint(comp, "REF")
        out["pads"] = pads_of(text)
        out["th"] = bool(meta.get("through_hole"))
        out["pad_names"] = list(meta.get("pads") or [])
    except Exception as e:  # noqa: BLE001
        out["fp_error"] = f"{type(e).__name__}: {e}"
    pkg = comp.get("packageDetail") or {}
    out["ee_package"] = pkg.get("title") or ""
    return out


def stock_fp(fp_id: str, project_dir) -> dict | None:
    from copper_kicad.kicad import env
    from copper_kicad.mcp.registry import ToolError
    lib, _, name = fp_id.partition(":")
    try:
        L = env.find_lib("fp", lib, project_dir)
    except ToolError:
        return None
    f = Path(L.path) / f"{name}.kicad_mod"
    if not f.exists():
        return None
    text = f.read_text(encoding="utf-8", errors="replace")
    pads = pads_of(text)
    m = re.search(r"\(attr\s+(\w+)", text)
    th = m.group(1) == "through_hole" if m else all(p["type"] == "thru_hole" for p in pads)
    return {"pads": pads, "th": th}


# ── comparisons ──────────────────────────────────────────────────────────
_SYN = [{"VCC", "VDD", "V+", "VS", "VIN", "VI", "IN", "INPUT", "VBUS", "VCCA", "VP", "3V3", "+3V3", "VDD33", "V3"},
        {"GND", "VSS", "V-", "EP", "PAD", "GNDA", "AGND", "PGND", "TAB", "VEE", "EPAD", "SHIELD", "SHELL", "EH", "MH"},
        {"K", "C", "CATHODE", "-", "NEG"}, {"A", "ANODE", "+", "POS"},
        {"VO", "VOUT", "OUT", "OUTPUT", "Q"}, {"ADJ", "GND/ADJ", "ADJ/GND", "FB"},
        {"EN", "CE", "SHDN", "ON/OFF", "CHIPEN", "CHIP_PU", "ENA", "ENABLE"}, {"NC", "N/C", "NOCONNECT", "DNC", "~", ""},
        {"G", "GATE"}, {"S", "SOURCE"}, {"D", "DRAIN"}, {"B", "BASE"}, {"E", "EMITTER"}, {"COLLECTOR", "C"},
        {"RST", "RESET", "NRST", "NRESET", "RES", "MCLR"}, {"CS", "SS", "NSS", "CSN", "NCS"},
        {"DO", "SO", "MISO", "IO1", "DOUT", "SDO"}, {"DI", "SI", "MOSI", "IO0", "DIN", "SDI"},
        {"CLK", "SCK", "SCLK", "SCL", "CK"}, {"DP", "D+", "UD+", "USBDP", "USB_DP"}, {"DM", "D-", "UD-", "USBDM", "USB_DM", "DN"},
        {"XI", "XTAL1", "OSCIN", "XIN", "OSC1", "XTALIN"}, {"XO", "XTAL2", "OSCOUT", "XOUT", "OSC2", "XTALOUT"}]


def _tokens(name: str) -> set:
    s = str(name or "").upper().replace("~{", "").replace("}", "").replace(" ", "")
    s = s.replace("#", "").lstrip("~")
    toks = {t for t in re.split(r"[/,()|\[\]]", s) if t}
    toks.add(s)
    more = set()
    for t in toks:
        more.add(re.sub(r"[^A-Z0-9+\-]", "", t))
        more.add(re.sub(r"^N(?=[A-Z]{2})", "", t))
        more.add(re.sub(r"\d+$", "", t))          # GND1/VCC2/IN1 → GND/VCC/IN
        more.add(re.sub(r"^(\d)([A-Z]+)$", r"\2\1", t))   # 1OUT ↔ OUT1
    return {t for t in toks | more if t}


def name_agree(a: str, b: str, num: str = "") -> str:
    """'same' | 'unknown' (reference gives no name) | 'differs'."""
    bs = str(b or "").strip()
    if not bs or bs == num or re.fullmatch(r"\d+|P\d+|PIN\d+", bs, re.I):
        return "unknown"
    as_ = str(a or "").strip()
    if not as_ or as_ == "~" or as_ == num or re.fullmatch(r"Pin_\d+", as_):
        return "unknown"
    if as_ in ("+", "-"):                        # op-amp inputs: stock '+' / '-' vs supplier '1IN+' / 'IN2-' / 'INA+'
        return "same" if (bs.endswith(as_) or bs.startswith(as_)) and not re.search(r"V|OUT", bs.upper()) else "differs"
    sa_, sb_ = re.fullmatch(r"([+-]?)V\w*?([+-]?)", as_.upper()), re.fullmatch(r"([+-]?)V\w*?([+-]?)", bs.upper())
    if sa_ and sb_ and (sa_.group(1) or sa_.group(2)) and (sb_.group(1) or sb_.group(2)):
        return "same" if (sa_.group(1) or sa_.group(2)) == (sb_.group(1) or sb_.group(2)) else "differs"   # V+ / VCC+ / -VSS
    ta, tb = _tokens(as_), _tokens(bs)
    if ta & tb:
        return "same"
    for g in _SYN:
        if ta & g and tb & g:
            return "same"
    for x, y in itertools.product(ta, tb):
        if len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x) or x in y or y in x):
            return "same"
    return "differs"


def compare_pins(stock: dict, ref: dict) -> dict:
    """stock/ref: {num: name}. → {'missing_in_stock', 'extra_in_stock', 'same', 'unknown', 'differs': [...]}"""
    s, r = set(stock), set(ref)
    res = {"missing_in_stock": sorted(r - s, key=_nat), "extra_in_stock": sorted(s - r, key=_nat),
           "same": 0, "unknown": 0, "differs": []}
    for n in sorted(s & r, key=_nat):
        v = name_agree(stock[n], ref[n], n)
        if v == "differs":
            res["differs"].append(f"{n}: stock '{stock[n]}' vs supplier '{ref[n]}'")
        else:
            res[v] += 1
    return res


def _nat(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def _centroids(pads: list[dict]) -> dict:
    acc: dict[str, list] = collections.defaultdict(list)
    for p in pads:
        acc[p["n"]].append((p["x"], p["y"]))
    return {n: (sum(x for x, _ in v) / len(v), sum(y for _, y in v) / len(v)) for n, v in acc.items()}


def _centre(pads):
    xs = [p["x"] for p in pads]
    ys = [p["y"] for p in pads]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


_ROTS = ((0, lambda x, y: (x, y)), (90, lambda x, y: (-y, x)), (180, lambda x, y: (-x, -y)), (270, lambda x, y: (y, -x)))


def compare_fp(stock: dict, ref: dict, stock_pins: dict | None = None, ref_pins: dict | None = None) -> dict:
    """Pad-level comparison of the stock footprint with the supplier's.

    geometry: every stock pad has a supplier pad near it and vice versa (both centred on their pad field, best
    of 4 rotations) — numbering-independent. numbering: at that rotation each stock pad N sits on a supplier
    pad N; where it sits on a differently numbered pad (SOT-223 tab = '2' in KiCad, '4' at the supplier) the
    two pins must carry the same name."""
    notes = []
    sp, rp = stock["pads"], ref.get("pads") or []
    if not rp or not sp:
        return {"verdict": "unknown", "notes": ["no supplier footprint to compare"]}
    sn, rn = {p["n"] for p in sp}, {p["n"] for p in rp}
    bad = False
    if stock["th"] != ref.get("th"):
        notes.append(f"mounting differs: stock is {'through-hole' if stock['th'] else 'SMD'}, the part is "
                     f"{'through-hole' if ref.get('th') else 'SMD'}")
        bad = True

    # thermal vias inside an exposed pad are extra same-numbered pads: drop them
    def main(pads):
        return [q for q in pads if not (q["type"] == "thru_hole" and any(o["type"] == "smd" and o["n"] == q["n"] for o in pads))]
    sp, rp = main(sp), main(rp)
    (sx, sy), (rx, ry) = _centre(sp), _centre(rp)
    best = None
    unpadded: set = set()
    for rot, f in _ROTS:
        pairs, worst, misnum = [], 0.0, 0
        rq = [(f(q["x"] - rx, q["y"] - ry), q["n"]) for q in rp]
        rhalf = [(q["w"] / 2, q["h"] / 2) if rot in (0, 180) else (q["h"] / 2, q["w"] / 2) for q in rp]
        shalf = {id(q): (q["w"] / 2, q["h"] / 2) for q in sp}
        for q in sp:
            ax, ay = q["x"] - sx, q["y"] - sy
            d, n = min((((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5, n) for (bx, by), n in rq)
            # prefer the same-numbered supplier pad when it is about as close
            same = [0.0 if (abs(ax - bx) <= hw + 0.2 and abs(ay - by) <= hh + 0.2) else ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
                    for ((bx, by), n2), (hw, hh) in zip(rq, rhalf) if n2 == q["n"]]
            if same and min(same) <= d + 0.3:
                d, n = min(same), q["n"]
            worst = max(worst, d)
            misnum += n != q["n"]
            pairs.append((q["n"], n))
        for (bx, by), n in rq:          # supplier pads with no stock pad anywhere near
            d = min(0.0 if (q["n"] == n and abs(q["x"] - sx - bx) <= max(shalf[id(q)]) + 0.2 and abs(q["y"] - sy - by) <= max(shalf[id(q)]) + 0.2)
                    else ((q["x"] - sx - bx) ** 2 + (q["y"] - sy - by) ** 2) ** 0.5 for q in sp)
            if d > 0.75 and n not in sn and str((ref_pins or {}).get(n, "")).upper() in ("EP", "TAB", "PAD", "EPAD", "GND", ""):
                unpadded.add(n)       # exposed pad the stock footprint leaves out
                continue
            worst = max(worst, d)
        key = (misnum, worst) if worst <= 0.75 else (10 ** 6, worst)
        if best is None or key < best[0]:
            best = (key, rot, worst, pairs)
    _, rot, worst, pairs = best
    if worst > 0.75:
        notes.append(f"pad layout differs from the supplier footprint by up to {worst:.2f} mm (best rotation {rot}°) — another package?")
        bad = True
    else:
        if worst > 0.4:
            notes.append(f"pads within {worst:.2f} mm of the supplier footprint (land-pattern variant)")
        if unpadded:
            notes.append(f"numbering: supplier exposed pad {sorted(unpadded, key=_nat)} has no pad in the stock footprint")
        renum = sorted({(a, b) for a, b in pairs if a != b})
        for a, b in renum:
            sa = (stock_pins or {}).get(a)
            rb = (ref_pins or {}).get(b)
            if sa is None or rb is None:
                notes.append(f"numbering: stock pad {a} lies on supplier pad {b} (no pin names to check)")
                continue
            # the supplier's own name for the stock pad's number says whether both pads are one net
            ra = (ref_pins or {}).get(a)
            if name_agree(sa, rb, b) != "differs" and (ra is None or name_agree(ra, rb, b) != "differs"):
                continue
            if rb.upper() in ("TAB", "EP", "PAD", "EPAD"):
                notes.append(f"numbering: stock pad {a} ('{sa}') is the supplier's unnamed tab pad {b} — check the tab's net in the datasheet")
                continue
            notes.append(f"numbering differs: stock pad {a} ('{sa}') lies on supplier pad {b} ('{rb}')")
            bad = True
    return {"verdict": "differs" if bad else "ok", "notes": notes, "max_offset_mm": round(worst, 3), "rotation": rot,
            "pad_numbers_equal": sn == rn}


def cathode_side(stock: dict, ref: dict, stock_pins: dict, ref_pins: dict) -> str | None:
    """Two-pad polarised part: is the cathode on the same side at 0° in KiCad's footprint and the supplier's?"""
    def k_of(pins):
        ks = [n for n, v in pins.items() if _tokens(v) & {"K", "CATHODE", "-", "NEG", "C"}]
        as_ = [n for n, v in pins.items() if _tokens(v) & {"A", "ANODE", "+", "POS"}]
        return ks[0] if len(ks) == 1 and len(as_) == 1 else None
    if len(stock_pins) != 2 or len(ref_pins) != 2:
        return None
    ks, kr = k_of(stock_pins), k_of(ref_pins)
    if not ks or not kr:
        return None
    try:
        a = next(q for q in stock["pads"] if q["n"] == ks)
        b = next(q for q in ref["pads"] if q["n"] == kr)
    except StopIteration:
        return None
    (sx, sy), (rx, ry) = _centre(stock["pads"]), _centre(ref["pads"])
    va, vb = (a["x"] - sx, a["y"] - sy), (b["x"] - rx, b["y"] - ry)
    dot = va[0] * vb[0] + va[1] * vb[1]
    if abs(va[0] * vb[1] - va[1] * vb[0]) > abs(dot):
        return "perpendicular"
    return "same" if dot > 0 else "opposite"


def compare_extent(stock: dict, ref: dict) -> dict:
    """Pad numbering differs (KiCad vs supplier naming): compare the overall pad field only."""
    def span(pads):
        xs = [p["x"] for p in pads]
        ys = [p["y"] for p in pads]
        return sorted((max(xs) - min(xs), max(ys) - min(ys)))
    a, b = span(stock["pads"]), span(ref["pads"])
    off = max(abs(a[0] - b[0]), abs(a[1] - b[1]))
    bad = off > 0.8          # mounting is not compared: a receptacle with through-hole shield legs is 'smd' in KiCad
    return {"verdict": "differs" if bad else "ok", "notes": [f"pad-field size differs by {off:.2f} mm"] if bad else [],
            "max_offset_mm": round(off, 3), "pad_numbers_equal": False}


# ── what stock symbol COULD have been used ───────────────────────────────
def _polarity(rec: dict) -> str:
    t = f"{rec.get('description') or ''} {rec.get('subcategory') or ''} {rec.get('category') or ''}".lower()
    if re.search(r"p[- ]?channel|\bpmos|\bp-ch", t):
        return "PMOS"
    if re.search(r"n[- ]?channel|\bnmos|\bn-ch", t):
        return "NMOS"
    if "pnp" in t:
        return "PNP"
    if "npn" in t:
        return "NPN"
    return ""


def _letters(ref_pins: dict, fam: str) -> str | None:
    want = "GSD" if fam.endswith("MOS") else "BEC"
    out = ""
    for n in sorted(ref_pins, key=_nat):
        nm = re.sub(r"[^A-Z]", "", ref_pins[n].upper())
        c = nm[:1]
        if nm in ("GATE", "SOURCE", "DRAIN", "BASE", "EMITTER", "COLLECTOR"):
            c = nm[0]
        if c not in want:
            return None
        out += c
    return out if sorted(out) == sorted(want) else None


def generic_candidates(rec: dict, ref: dict) -> list[tuple[str, str]]:
    """[(lib_id, why)] — generic stock symbols that could stand for this part, judged from the reference pinout."""
    kind = rec.get("kind") or ""
    pins = ref.get("pins") or {}
    n = len(pins)
    out = []
    if kind in ("mosfet", "bjt") and n == 3:
        fam = _polarity(rec)
        if fam:
            order = _letters(pins, fam)
            if order:
                for lib in ("Transistor_FET", "Transistor_BJT", "Device"):
                    out.append((f"{lib}:Q_{fam}_{order}", f"generic {fam} with pin order {order}"))
    if kind == "crystal":
        if n == 2:
            out.append(("Device:Crystal", "generic 2-pin crystal"))
        elif n == 4:
            out += [("Device:Crystal_GND24", "generic 4-pad crystal, case on 2+4"), ("Device:Crystal_GND23", "4-pad variant")]
    if kind == "switch":
        out.append(("Switch:SW_Push", "generic push button"))
        if n == 3:
            out.append(("Switch:SW_SPDT", "generic SPDT"))
    if kind in ("tantalum", "electrolytic"):
        out.append(("Device:C_Polarized", "generic polarised capacitor"))
    if kind in ("inductor",) and n == 2:
        out.append(("Device:L", "generic inductor"))
    if kind == "ferrite" and n == 2:
        out.append(("Device:FerriteBead", "generic ferrite bead"))
    if kind == "fuse" and n == 2:
        out.append(("Device:Polyfuse", "generic PTC fuse"))
    if kind == "led" and n == 2:
        out.append(("Device:LED", "generic LED"))
    if kind in ("diode", "schottky", "zener", "tvs", "esd") and n == 2:
        out.append((f"Device:{ {'schottky': 'D_Schottky', 'zener': 'D_Zener', 'tvs': 'D_TVS', 'esd': 'D_TVS'}.get(kind, 'D')}", "generic diode"))
    if kind in ("header", "connector") and n and all(re.fullmatch(r"\d+", k) for k in pins):
        text = f"{rec.get('description') or ''} {rec.get('mfr') or ''}"
        m = re.search(r"(?<![\d.])([12])\s*[x*]\s*(\d{1,2})\s*P", text, re.I)
        rows, per = (int(m.group(1)), int(m.group(2))) if m else (1, n)
        if not m:
            m2 = re.search(r"(?<![\d.])(\d{1,2})\s*P(?:in)?\b", text, re.I)
            per = int(m2.group(1)) if m2 else n
        poles = rows * per
        if poles == n and rows == 1:
            out.append((f"Connector_Generic:Conn_01x{n:02d}", f"generic 1x{n} connector"))
        elif poles == n and rows == 2:
            scheme = two_row_scheme(ref.get("pads") or [], per)
            if scheme:
                out.append((f"Connector_Generic:Conn_02x{per:02d}_{scheme}", f"generic 2x{per} connector, supplier pads numbered {scheme}"))
        elif poles < n and rows == 1:
            out.append((f"Connector_Generic_MountingPin:Conn_01x{poles:02d}_MountingPin",
                        f"generic 1x{poles} with mounting pin ({n - poles} mechanical pads)"))
    if kind == "usb_connector":
        out += [("Connector:USB_C_Receptacle_USB2.0_16P", "stock USB-C 2.0 16P"), ("Connector:USB_C_Receptacle_USB2.0_14P", "stock USB-C 2.0 14P"),
                ("Connector:USB_C_Receptacle_PowerOnly_6P", "stock USB-C power-only 6P"), ("Connector:USB_B_Micro", "stock micro-B"),
                ("Connector:USB_A", "stock USB-A")]
    return out


_FP_INDEX: list | None = None


def mpn_footprints(mpn: str) -> list[str]:
    """Stock footprints whose NAME contains the MPN (KiCad ships exact footprints for popular connectors/modules)."""
    global _FP_INDEX
    from copper_kicad.kicad import env
    from copper_kicad.parts import importer
    if _FP_INDEX is None:
        _FP_INDEX = []
        for lib in env.lib_tables("fp"):
            if lib.scope == "project" or not lib.exists:
                continue
            try:
                for f in os.scandir(lib.path):
                    if f.name.endswith(".kicad_mod"):
                        _FP_INDEX.append((importer._alnum(f.name[:-10]), f"{lib.name}:{f.name[:-10]}"))
            except OSError:
                pass
    key = importer._alnum(mpn)
    if len(key) < 6:
        return []
    return sorted((fid for k, fid in _FP_INDEX if key in k), key=len)[:3]


def _subseq(a: str, b: str) -> bool:
    it = iter(b)
    return all(ch in it for ch in a)


def two_row_scheme(pads: list[dict], per: int) -> str | None:
    """How the supplier numbers a 2-row header, as a KiCad Conn_02xNN suffix (the symbol must draw the same)."""
    c = _centroids(pads)
    if len(c) != 2 * per or not all(str(k) in c for k in range(1, 2 * per + 1)):
        return None
    xs = {round(v[0], 1) for v in c.values()}
    ys = {round(v[1], 1) for v in c.values()}
    if len(ys) == 2 and len(xs) == per:
        row, col = (lambda v: round(v[1], 1)), (lambda v: round(v[0], 1))
    elif len(xs) == 2 and len(ys) == per:
        row, col = (lambda v: round(v[0], 1)), (lambda v: round(v[1], 1))
    else:
        return None
    p1 = c["1"]
    if per == 1:
        return "Odd_Even"
    if row(c["2"]) != row(p1) and col(c["2"]) == col(p1):
        return "Odd_Even"
    if row(c["2"]) == row(p1):
        last, nxt = c[str(2 * per)], c[str(per + 1)]
        if col(last) == col(p1) and row(last) != row(p1):
            return "Counter_Clockwise"
        if col(nxt) == col(p1) and row(nxt) != row(p1):
            return "Top_Bottom"
    return None


def family_candidates(mpn: str) -> list[tuple[str, str]]:
    """Stock symbols named like a PREFIX of the MPN (LM358DR2G → LM358, STM32F103C8T6 → STM32F103C8Tx) — what an
    MPN lookup that understood package/ordering suffixes would reach. Longest names first."""
    from copper_kicad.parts import importer
    idx = importer._sym_index()
    key = importer._alnum(mpn)
    raw = re.sub(r"[^A-Z0-9_.+-]", "", str(mpn or "").upper())
    out = []
    for cut in range(len(key), 3, -1):
        for lid in idx["exact"].get(key[:cut], []):
            out.append((lid, f"MPN prefix '{key[:cut]}' of '{mpn}'"))
        if len(out) >= 6:
            break
    for lid, rx, name in idx["wild"]:
        body = rx[1:-1]
        if len(name) >= 5 and re.match(body, raw):
            out.append((lid, f"wildcard family symbol {name}"))
    if len(key) >= 5:
        ext = [lid for k, lids in idx["exact"].items() if k.startswith(key) and k != key for lid in lids]
        if 0 < len(ext) <= 3:
            out += [(lid, f"stock symbol name extends the MPN '{mpn}'") for lid in ext]
    wild_names = {(lid, lid.split(":", 1)[1]) for lids in idx["exact"].values() for lid in lids if "x" in lid.split(":", 1)[1]}
    for lid, name in sorted(wild_names):
        lit = re.sub(r"[^A-Z0-9]", "", name.replace("x", "").upper())
        if len(lit) >= 6 and key[:5] == lit[:5] and _subseq(lit, key) and len(key) - len(lit) <= name.count("x") + 3:
            out.append((lid, f"wildcard family symbol {name} (ordering letters differ)"))
    seen, uniq = set(), []
    for lid, why in out:
        if lid not in seen:
            seen.add(lid)
            uniq.append((lid, why))
    return uniq[:10]


def judge_candidate(lid: str, why: str, rec: dict, ref: dict, pr_dir, mpn_fps=()) -> dict:
    from copper_kicad.parts import importer
    sym = importer.stock_symbol(lid, pr_dir)
    if sym is None:
        return {"lib_id": lid, "why": why, "exists": False}
    d = {"lib_id": lid, "why": why, "exists": True, "default_footprint": sym.get("footprint") or ""}
    if sym.get("stacked"):
        d["stacked_pin_numbers"] = True
    for fid in mpn_fps:          # KiCad's own footprint for exactly this MPN: its pad numbering is the stock symbol's
        pads = importer.footprint_pads(fid, pr_dir) or set()
        if set(sym["pins"]) <= pads and len(pads) - len(sym["pins"]) <= 2:
            d.update(pinout="matches", mpn_footprint=fid, with_supplier_footprint=False)
            sf = stock_fp(fid, pr_dir)
            if sf and ref.get("pads"):
                d["stock_footprint_vs_supplier"] = compare_extent(sf, ref)
            return d
    rp = ref.get("pins")
    if rp is None:
        d["pinout"] = "unknown (no supplier symbol)"
        return d
    cmp_ = compare_pins(sym["pins"], rp)
    d["pins"] = cmp_
    ref_pads = set(ref.get("pad_names") or [])
    # a stock symbol may leave the supplier's mechanical / duplicate pads off; it must not lack an electrical pin
    lacking = [n for n in cmp_["missing_in_stock"] if name_agree("GND", rp.get(n, ""), n) != "same" and rp.get(n, "").upper() not in ("EP", "NC", "MH", "EH", "SH")]
    if (rec.get("lcsc"), lid) in REVIEWED:
        d["pinout"] = "matches"
        d["reviewed"] = REVIEWED[(rec.get("lcsc"), lid)]
    elif generic_ok(lid, sym["pins"], rp):
        d["pinout"] = "matches"
    elif lid.startswith("Connector_Generic_MountingPin:"):
        d["pinout"] = "needs pad mapping"
    elif lid.startswith("Connector:USB_") and usb_fit(lid, sym["pins"], rp):
        d["pinout"] = "needs pad mapping"
    elif lid == "Switch:SW_Push" and len(rp) in (4, 5, 6):
        d["pinout"] = "needs pad mapping"          # which supplier pads are the same contact is not in the symbol
    elif lacking or cmp_["extra_in_stock"] or cmp_["differs"]:
        d["pinout"] = "differs"
    else:
        d["pinout"] = "matches" if cmp_["same"] or not cmp_["unknown"] else "numbers match, names unverifiable"
    d["with_supplier_footprint"] = bool(ref_pads) and set(sym["pins"]) <= ref_pads
    fp = d["default_footprint"]
    if fp:
        ok, notes, _ = importer.verify_stock(lid, fp, rec, pr_dir)
        d["verify_stock"] = {"ok": ok, "notes": notes}
        if not ok and d.get("stacked_pin_numbers") and "have no pad" in " ".join(notes):
            d["verify_stock"]["blocked_by_stacked_pins"] = True
        sf = stock_fp(fp, pr_dir)
        if sf and ref.get("pads"):
            d["stock_footprint_vs_supplier"] = compare_fp(sf, ref, sym["pins"], rp)
    return d


def expand_stacked(pins: dict, flag: dict | None = None) -> dict:
    """KiCad 10 stacked pin numbers: one pin numbered '[1,15,38,39]' or '[1-4]' stands for all of them."""
    out = {}
    for n, v in pins.items():
        m = re.fullmatch(r"\[(.+)\]", n)
        if not m:
            out[n] = v
            continue
        if flag is not None:
            flag["stacked_pin_numbers"] = True
        for part in m.group(1).split(","):
            r = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", part)
            if r:
                for k in range(int(r.group(1)), int(r.group(2)) + 1):
                    out[str(k)] = v
            else:
                out[part.strip()] = v
    return out


def usb_fit(lid: str, stock_pins: dict, ref_pins: dict) -> bool:
    """A stock USB symbol of the right connector family for this part (pad names differ per manufacturer)."""
    names = " ".join(ref_pins.values()).upper()
    nums = set(ref_pins)
    c_like = bool(re.search(r"\bCC1\b|\bCC\b", names)) or any(re.fullmatch(r"[AB]\d+([AB]\d+)?", n) for n in nums)
    has_data = bool(re.search(r"D\+|DP", names))
    has_sbu = "SBU" in names
    if "USB_C_Receptacle_USB2.0_16P" in lid:
        return c_like and has_data and has_sbu
    if "USB_C_Receptacle_USB2.0_14P" in lid:
        return c_like and has_data and not has_sbu
    if "PowerOnly_6P" in lid:
        return c_like and not has_data
    if lid.endswith("USB_B_Micro"):
        return not c_like and bool(re.search(r"\bID\b", names))
    if lid.endswith("USB_A"):
        return not c_like and has_data and not re.search(r"\bID\b", names) and len([v for v in ref_pins.values() if v.upper() not in ("EH", "SHELL", "SH", "MH", "")]) <= 4
    return False


def generic_ok(lid: str, stock_pins: dict, ref_pins: dict) -> bool:
    """Generic symbols whose pin NAMES are not comparable by text but whose pinout is right by construction."""
    if set(stock_pins) != set(ref_pins):
        return False
    name = lid.split(":", 1)[1]
    if lid.startswith("Connector_Generic:Conn_0"):
        return True
    if name in ("Crystal", "L", "FerriteBead", "Polyfuse", "R", "C", "SW_Push") and len(stock_pins) == 2:
        return True
    if name.startswith("Crystal_GND"):
        gnd = {n for n, v in ref_pins.items() if re.fullmatch(r"GND\d*|G|CASE|NC|SHELL", v.upper())}
        return gnd == {n for n, v in stock_pins.items() if v.upper() == "G"}
    return False


# Pin-name disagreements a person checked against the datasheets: the two drawings name the same functions
# differently (TI vs Nexperia vs KiCad convention). Keyed (C-number, stock symbol).
REVIEWED = {
    ("C7512", "Transistor_Array:ULN2003A"): "TI names 1B..7B / 1C..7C / E = KiCad I1..I7 / O1..O7 / GND",
    ("C5947", "74xx:74HC595"): "Nexperia Q0..Q7, Q7S, MR#, SHCP, STCP, DS = TI QA..QH, QH', SRCLR, SRCLK, RCLK, SER",
    ("C21379", "Analog_Switch:CD4051B"): "TI 'CHANNELS IN/OUT n' / 'COM OUT/IN' = KiCad Xn / X",
    ("C2913202", "RF_Module:ESP32-S3-WROOM-1"): "IO19 / IO20 are USB_D- / USB_D+ on the ESP32-S3",
    ("C37704", "Diode:BAT54C"): "BAT54C SOT-23: 1, 2 anodes, 3 common cathode (KiCad symbol pins are unnamed)",
}
# Chosen stock symbols whose audit flag a person checked and found harmless.
REVIEWED_OK = {
    "C6186": "AMS1117 tab is VOUT: the supplier's pad 4 = KiCad's second pad '2' (SOT-223-3_TabPin2)",
    "C347222": "AMS1117 tab is VOUT: the supplier's pad 4 ('TAB') = KiCad's second pad '2' (SOT-223-3_TabPin2)",
    "C2040": "VREG_VIN vs VREG_IN: same pin 44",
    "C84681": "CH340C pin 8 is NC (the supplier drawing reuses the CH340G name)",
    "C351410": "DW01A pin 2 is the current-sense pin: CS (KiCad) = VM (some datasheets)",
    "C9864": "EN vs ENA, GNDPAD vs PowerPAD: same pins",
    "C9002": "Crystal_GND24 names its case pins 'G'; the supplier names pads 2 and 4 'GND'",
    "C13738": "Crystal_GND24 names its case pins 'G'; the supplier names pads 2 and 4 'GND'",
}


# Chosen stock symbols whose audit PROBLEM a person read and found to be an artefact of this audit's own comparison
# (the selection's own checks, which map pads by position, passed). The problems stay in the JSON under
# 'reviewed_problems'. Keyed C-number → (text every problem line must contain one of, reason).
REVIEWED_PROBLEMS = {
    "C2913202": (("'USB_D-' vs supplier 'IO19'",),
                 "IO19 / IO20 ARE USB_D- / USB_D+ on the ESP32-S3; this audit's loose 'permuted' test matched IO19 to the stock pin IO1"),
    "C71136": (("pad layout differs from the supplier footprint",),
               "SOT-89 pad 2 (lead + tab) is a custom-shaped pad: KiCad anchors it at the lead, the supplier at the tab, and "
               "this audit compares anchors. Copper extents read by hand (after 180°): leads 1/3 at x 1.30..2.60 (KiCad) vs "
               "0.58..2.15 (supplier), same y ±1.5; pad 2 spans -2.0..2.6 vs -3.2..2.2 — one SOT-89 with different toe lengths. "
               "Pins OUT/GND/IN = the supplier's OUT/GND/IN."),
    "C165948": (("mounting differs",),
                "KiCad's HRO TYPE-C-31-M-12 footprint has one pad per contact (A1…B12) and the shell as 'SH'; the supplier merges "
                "A1+B12 / A4+B9 / B1+A12 / B4+A9 into compound pads and numbers the shell 1-4. Pad positions read by hand: stock "
                "A1/B12 x=-3.25 on supplier A1B12 x=-3.20, A4/B9 -2.45 on A4B9 -2.40, A5..A8 / B5..B8 identical, SH on 1-4. The "
                "'mounting' line is the 4 through-hole shell legs (KiCad marks the footprint SMD)."),
}


# ── one part ─────────────────────────────────────────────────────────────
def permuted(stock_pins: dict, ref_pins: dict) -> bool:
    """At least two pins whose supplier name is the name of a DIFFERENT stock pin (and not of their own)."""
    n = 0
    for k in set(stock_pins) & set(ref_pins):
        if name_agree(stock_pins[k], ref_pins[k], k) != "differs":
            continue
        if any(m != k and name_agree(v, ref_pins[k], k) == "same" for m, v in stock_pins.items()):
            n += 1
    return n >= 2


def unidirectional(rec: dict) -> bool | None:
    """TVS / ESD diode direction from the catalogue text, else from the usual MPN convention (…A uni, …CA / …B bi)."""
    if rec.get("kind") not in ("tvs", "esd"):
        return None
    t = f"{rec.get('description') or ''}".lower()
    if "bidirectional" in t or "bi-directional" in t:
        return False
    if "unidirectional" in t or "uni-directional" in t:
        return True
    m = str(rec.get("mfr") or "").upper()
    if re.fullmatch(r"(SMF|SMAJ|SMBJ|SMCJ|P4SM[AB]|P6SM[AB]|1\.5SM[CB]|SMA6J|SMB6J)[\d.]+(C?A)(?:[-/].*)?", m):
        return not re.search(r"CA(?:[-/].*)?$", m)
    return None


def _reject_class(note: str) -> str:
    if "has no default footprint" in note:
        return "stock symbol has no default footprint"
    if "have no pad in" in note:
        return "stock symbol pins lack pads in the footprint"
    if "pins but" in note and "pads" in note:
        return "package pin-count check failed"
    if "not found in footprint libraries" in note:
        return "rule/default footprint does not exist in the stock footprint libraries"
    if "not found in symbol libraries" in note:
        return "stock symbol not found"
    return "other: " + note[:60]


def audit(code: str, group: str, note: str, pr) -> dict:
    from copper_kicad.mcp.registry import ToolError
    from copper_kicad.parts import importer
    row: dict = {"lcsc": code, "group": group, "note": note}
    try:
        rec = importer.part_record(code)
    except Exception as e:  # noqa: BLE001
        rec = {}
        row["record_error"] = f"{type(e).__name__}: {e}"
    row.update(mpn=rec.get("mfr"), package=rec.get("package"), kind=rec.get("kind"), jlc_class=rec.get("class"),
               record_source=rec.get("source") or ("offline table" if rec else None))
    try:
        r = importer.resolve_lcsc(pr, code, with_3d=False)
    except ToolError as e:
        row.update(chosen="error", error=f"{e.code}: {e.message}")
        return row
    except Exception as e:  # noqa: BLE001
        row.update(chosen="error", error=f"{type(e).__name__}: {e}")
        return row
    if not rec:        # EasyEDA-only part: resolve_lcsc built the record itself
        rec = {"mfr": r.get("mpn"), "package": r.get("package"), "kind": "other", "description": (r.get("fields") or {}).get("Description", "")}
        row.update(mpn=rec["mfr"], package=rec["package"], kind="other", record_source="easyeda only")
    row.update(chosen="stock" if r.get("source") == "kicad-stock" else "converted", lib_id=r["lib_id"],
               footprint=r.get("footprint"), why=r.get("why"), footprint_source=r.get("footprint_source"),
               selection=r.get("selection"),
               rejected=[w for w in r.get("warnings") or [] if w.startswith("stock candidate rejected")])
    rec = dict(rec, lcsc=code)
    ref = reference(code, rec.get("kind") or "")
    row["reference"] = {k: ref.get(k) for k in ("error", "sym_error", "fp_error", "ee_package", "units") if ref.get(k)}
    row["reference"]["pins"] = len(ref.get("pins") or {})
    row["reference"]["pads"] = len({p["n"] for p in ref.get("pads") or []})

    if row["chosen"] == "stock":
        flag: dict = {}
        raw = importer.stock_symbol(r["lib_id"], pr.dir) or {"pins": {}}
        spins = dict(raw["pins"])
        a: dict = {}
        problems, review = [], []
        rp = ref.get("pins")
        if rp is not None:
            a["pins"] = cp = compare_pins(spins, rp)
            for n in cp["missing_in_stock"]:
                nm = rp.get(n, "")
                dup = any(name_agree(v, nm, n) == "same" for v in spins.values()) if nm else False
                if dup or nm.upper() in ("EP", "NC", "MH", "EH", "SH", ""):
                    review.append(f"supplier pin {n} ('{nm}') has no stock pin — duplicate / exposed pad / unnamed")
                else:
                    problems.append(f"supplier pin {n} ('{nm}') has no stock pin")
            if cp["extra_in_stock"]:
                review.append(f"stock symbol has pins {cp['extra_in_stock'][:8]} the supplier symbol lacks")
            nonpolar = len(spins) == 2 and ({v.upper() for v in spins.values()} <= {"A1", "A2", "~", "", "1", "2"})
            polar_ref = len(rp) == 2 and sum(1 for v in rp.values() if _tokens(v) & {"K", "A", "+", "-", "CATHODE", "ANODE"}) == 2
            uni = unidirectional(rec)
            a["unidirectional"] = uni
            if nonpolar and polar_ref and uni and r["lib_id"] not in ("Device:R", "Device:C", "Device:L"):
                problems.append(f"the part is polarised (supplier pins {dict(sorted(rp.items()))}) but {r['lib_id']} is a non-polarised symbol "
                                f"(pins {dict(sorted(spins.items()))}): nothing in the schematic says which way round it goes")
            elif nonpolar and polar_ref and uni is False:
                pass          # bidirectional part: the supplier's K/A names carry no meaning
            elif cp["differs"] and not r["lib_id"].startswith("Device:") and permuted(spins, rp):
                # a part-specific symbol whose functions sit on other pin numbers than the supplier's: another
                # package / pinout variant of the same family (AP7361C-33E vs -33ER, TO-92 vs SOT-23 transistor)
                problems.append("pinout is PERMUTED vs the supplier drawing: " + "; ".join(cp["differs"][:6]))
            elif cp["differs"]:
                rev = len(spins) == 2 and len(cp["differs"]) == 2
                review.append(("pin NUMBERING is reversed vs the supplier drawing (the netlist is still self-consistent with the "
                               "stock footprint): " if rev else "pin names differ: ") + "; ".join(cp["differs"][:6]))
        else:
            review.append("no supplier symbol to compare against")
        sf = stock_fp(r.get("footprint") or "", pr.dir)
        if sf is None:
            problems.append("stock footprint could not be read")
        elif ref.get("pads"):
            a["footprint"] = cf = compare_fp(sf, ref, spins, rp)
            if cf["verdict"] == "differs":
                problems += cf["notes"]
            else:
                review += [n for n in cf["notes"] if "numbering" in n]
            if rp:
                side = cathode_side(sf, ref, spins, rp)
                a["cathode_at_0deg"] = side
                if side == "opposite":
                    review.append("cathode is on the opposite side at 0° in the supplier footprint: the placement file needs a 180° "
                                  "correction for this part unless the assembly rotation table already applies one")
        else:
            review.append("no supplier footprint to compare against")
        if not problems and code in REVIEWED_OK:
            a["reviewed"] = REVIEWED_OK[code]
            review = []
        if problems and code in REVIEWED_PROBLEMS and any(k in p for k in REVIEWED_PROBLEMS[code][0] for p in problems):
            a["reviewed"] = REVIEWED_PROBLEMS[code][1]
            a["reviewed_problems"] = problems + review
            problems, review = [], []
        a["verdict"] = "WRONG?" if problems else ("review" if review else "ok")
        a["problems"], a["review"] = problems, review
        row["audit"] = a
        return row

    # converted: why, and what stock symbol would have been right
    sc = importer.stock_candidate(rec)
    named = importer.find_stock_symbols(rec.get("mfr") or "")
    row["rule_candidate"] = sc["lib_id"] if sc else None
    row["mpn_named"] = named
    cands = []
    seen = set()
    fps = mpn_footprints(rec.get("mfr") or "")
    row["mpn_footprints"] = fps
    for lid, why in family_candidates(rec.get("mfr") or "") + generic_candidates(rec, ref):
        if lid in seen:
            continue
        seen.add(lid)
        j = judge_candidate(lid, why, rec, ref, pr.dir, fps)
        if j.get("exists"):
            cands.append(j)
    good = [c for c in cands if c.get("pinout") == "matches"]
    row["alternatives"] = cands
    full = [c for c in good if ((c.get("verify_stock") or {}).get("ok") or (c.get("verify_stock") or {}).get("blocked_by_stacked_pins")) and (c.get("stock_footprint_vs_supplier") or {}).get("verdict") == "ok"
            and (c.get("stock_footprint_vs_supplier") or {}).get("pad_numbers_equal")]
    symonly = [c for c in good if c.get("with_supplier_footprint")]
    if row["rejected"]:
        row["miss"] = "candidate rejected: " + _reject_class(row["rejected"][0])
        if any((c.get("verify_stock") or {}).get("blocked_by_stacked_pins") for c in cands):
            row["miss"] = "candidate rejected: stacked pin numbers '[1,15,38,39]' (KiCad 10) are not expanded by the pad check"
    elif not named and not sc:
        row["miss"] = "no candidate: MPN is not a stock symbol name and no kind/package rule applies"
    else:
        row["miss"] = "candidate vanished"      # should not happen
    exact_fp = [c for c in good if c.get("mpn_footprint") and (c.get("stock_footprint_vs_supplier") or {}).get("verdict") != "differs"]
    remap = [c for c in cands if c.get("pinout") == "needs pad mapping"]
    if exact_fp:
        row["could_be"] = {"level": "stock symbol + stock footprint", "lib_id": exact_fp[0]["lib_id"],
                           "footprint": exact_fp[0]["mpn_footprint"], "via": exact_fp[0]["why"] + " + stock footprint named after the MPN"}
    elif full:
        row["could_be"] = {"level": "stock symbol + stock footprint", "lib_id": full[0]["lib_id"],
                           "footprint": full[0]["default_footprint"], "via": full[0]["why"]}
    elif symonly:
        row["could_be"] = {"level": "stock symbol + converted footprint", "lib_id": symonly[0]["lib_id"], "via": symonly[0]["why"]}
    elif good:
        row["could_be"] = {"level": "stock symbol (pad numbering needs mapping)", "lib_id": good[0]["lib_id"], "via": good[0]["why"]}
    elif remap:
        row["could_be"] = {"level": "stock symbol (pad numbering needs mapping)", "lib_id": remap[0]["lib_id"], "via": remap[0]["why"]}
    else:
        row["could_be"] = None
    return row


def _via_class(via: str) -> str:
    if via.startswith("stock symbol name extends"):
        return "MPN is shorter than the stock symbol name (TP4056 → TP4056-42-ESOP8)"
    if via.startswith("MPN prefix"):
        return "MPN lookup stops at the package/ordering suffix (family symbol exists)"
    if via.startswith("wildcard"):
        return "MPN lookup misses the wildcard family symbol"
    return "no rule maps this kind to its generic stock symbol"


# ── report ───────────────────────────────────────────────────────────────
def report(rows: list[dict]) -> str:
    L = []
    w = L.append
    w(f"{'LCSC':<10} {'group':<14} {'MPN':<26} {'package':<18} {'chosen':<10} symbol / detail")
    for r in rows:
        head = f"{r['lcsc']:<10} {r['group']:<14} {str(r.get('mpn') or '?')[:25]:<26} {str(r.get('package') or '?')[:17]:<18} {r['chosen']:<10}"
        if r["chosen"] == "error":
            w(f"{head} {r['error'][:110]}")
            continue
        w(f"{head} {r['lib_id']}  |  {r.get('footprint')}")
        if r["chosen"] == "stock":
            sel = r.get("selection") or {}
            w(f"{'':<12}why: [{sel.get('match')}; footprint {r.get('footprint_source')}; {sel.get('verified')}] {str(r.get('why'))[:230]}")
            a = r["audit"]
            if a["verdict"] != "ok":
                for p in a["problems"]:
                    w(f"{'':<12}!! {p}")
                for p in a["review"]:
                    w(f"{'':<12}?  {p}")
        else:
            w(f"{'':<12}miss: {r['miss']}")
            w(f"{'':<12}fallback: {str((r.get('selection') or {}).get('fallback'))[:300]}")
            for x in r["rejected"]:
                w(f"{'':<12}   {x[:170]}")
            cb = r.get("could_be")
            if cb:
                w(f"{'':<12}could be: {cb['lib_id']} [{cb['level']}] via {cb['via']}")
    w("")
    w("== stock-symbol rate by group ==")
    groups: dict[str, list] = collections.defaultdict(list)
    for r in rows:
        groups[r["group"]].append(r)
    w(f"{'group':<20} {'n':>3} {'stock':>6} {'(conv fp)':>9} {'conv':>5} {'err':>4} {'rate':>6}   {'stock ok':>8} {'review':>6} {'WRONG?':>6}   {'conv, stock symbol possible':>28}")
    tot = collections.Counter()
    for g, rs in groups.items():
        c = collections.Counter(r["chosen"] for r in rs)
        v = collections.Counter(r["audit"]["verdict"] for r in rs if r["chosen"] == "stock")
        poss = sum(1 for r in rs if r["chosen"] == "converted" and r.get("could_be"))
        n = len(rs)
        cf = sum(1 for r in rs if r["chosen"] == "stock" and r.get("footprint_source") == "converted")
        w(f"{g:<20} {n:>3} {c['stock']:>6} {cf:>9} {c['converted']:>5} {c['error']:>4} {100 * c['stock'] / n:>5.0f}%   {v['ok']:>8} {v['review']:>6} {v['WRONG?']:>6}   {poss:>28}")
        tot.update(n=n, stock=c["stock"], cf=cf, conv=c["converted"], err=c["error"], ok=v["ok"], review=v["review"], wrong=v["WRONG?"], poss=poss)
    w(f"{'TOTAL':<20} {tot['n']:>3} {tot['stock']:>6} {tot['cf']:>9} {tot['conv']:>5} {tot['err']:>4} {100 * tot['stock'] / max(1, tot['n']):>5.0f}%   {tot['ok']:>8} {tot['review']:>6} {tot['wrong']:>6}   {tot['poss']:>28}")
    w("('(conv fp)' = stock symbol on the footprint converted from the supplier drawing; counted in 'stock')")
    w("")
    w("== why converted parts did not get a stock symbol (parts each reason costs) ==")
    reasons = collections.Counter()
    ex: dict[str, list] = collections.defaultdict(list)
    for r in rows:
        if r["chosen"] != "converted":
            continue
        cb = r.get("could_be")
        if cb:
            if r["miss"].startswith("candidate rejected"):
                key = f"{r['miss']} — but {cb['level']} fits"
            else:
                key = f"{_via_class(cb['via'])} → {cb['level']}"
        else:
            key = ("no stock symbol matches the part's pinout (conversion is the right call)"
                   if not r["miss"].startswith("candidate rejected") else f"{r['miss']} — and no other stock symbol fits")
        reasons[key] += 1
        ex[key].append(f"{r['lcsc']} {r.get('mpn')}" + (f"→{cb['lib_id']}" if cb else ""))
    for k, n in reasons.most_common():
        w(f"{n:>3}  {k}")
        w(f"       {'; '.join(ex[k])[:400]}")
    w("")
    w("== chosen stock symbols flagged by the audit ==")
    for r in rows:
        if r["chosen"] == "stock" and r["audit"]["verdict"] != "ok":
            a = r["audit"]
            w(f"{a['verdict']:<7} {r['lcsc']} {r.get('mpn')} ({r.get('package')}) → {r['lib_id']} | {r.get('footprint')}")
            for p in a["problems"] + a["review"]:
                w(f"          {p}")
    return "\n".join(L)


# ── whole offline table (no network, no supplier drawing) ────────────────
_SMD_PKG = re.compile(r"^(SOT|SOD|SOIC|SOP|SSOP|TSSOP|MSOP|QFN|DFN|LQFP|TQFP|QFP|SMA|SMB|SMC|SC-|0[2468]0[1-6]|1206|1210|ESOP|LGA|BGA|WSON|SON|UFQFPN|VQFN|WQFN|TO-252|TO-263|DO-214)", re.I)


def _pkg_token(pkg: str) -> str:
    t = re.split(r"[(,]", str(pkg or ""))[0].strip().upper()
    t = re.sub(r"-(\d+)L$", r"-\1", t)
    return re.sub(r"[^A-Z0-9]", "", t)


def _pkg_in_fp(pkg: str, fp: str) -> bool:
    """Loose: does the stock footprint's name mention the catalogue package (SOT-23 ↔ SOT-23, SOIC-8 ↔ SOIC-8/SO-8)?"""
    from copper_kicad.parts import common as C
    name = re.sub(r"[^A-Z0-9]", "", fp.split(":", 1)[-1].upper())
    tok = _pkg_token(pkg)
    if not tok:
        return True
    if C.norm_pkg(pkg) in C.CHIP_SIZES:
        return C.norm_pkg(pkg) in fp
    alts = {tok, tok.replace("SOP", "SOIC"), tok.replace("SOP", "SO"), tok.replace("SOIC", "SO"), tok.replace("ESOP", "SOIC"),
            tok.replace("SOT233", "SOT23"), tok.replace("DO214AC", "SMA"), tok.replace("DO214AA", "SMB"), tok.replace("DO214AB", "SMC"),
            tok.replace("LQFN", "QFN"), tok.replace("UFQFPN", "QFN"), tok.replace("WQFN", "QFN"), tok.replace("VQFN", "QFN"),
            tok.replace("SOD123FL", "SOD123F"), tok.replace("SMAF", "SMF"), tok.replace("TQFP", "QFP"), tok.replace("LQFP", "QFP"),
            re.sub(r"^(SOT23)\d$", r"\1", tok), tok.replace("SOT2233", "SOT223"), tok.replace("SOT893", "SOT89"),
            tok.replace("HTSSOP", "TSSOP"), tok.replace("SOIC", "SOP"), tok.replace("SC70", "SOT323")}
    return any(a and a in name for a in alts)


def table_sweep(pr) -> tuple[list[dict], str]:
    """Every part of the bundled offline table through the importer's stock rule (prefer='stock', live=False): the
    stock rate by kind over ~1800 Basic/Preferred parts, and the chosen stock footprints whose NAME does not fit the
    catalogue package (the S8050-in-SOT-23 → TO-92 class of error), for a person to read."""
    from copper_kicad.mcp.registry import ToolError
    from copper_kicad.parts import basic, importer
    rows = []
    recs = {p["lcsc"]: p for p in basic.all_parts()}
    for rec in basic.all_parts():
        code = rec["lcsc"]
        row = {"lcsc": code, "mpn": rec.get("mfr"), "package": rec.get("package"), "kind": rec.get("kind") or "other", "jlc_class": rec.get("class")}
        try:
            r = importer.resolve_lcsc(pr, code, prefer="stock", live=False, with_3d=False)
            row.update(chosen="stock", lib_id=r["lib_id"], footprint=r["footprint"], why=r.get("why"),
                       footprint_source=r.get("footprint_source"), match=(r.get("selection") or {}).get("match"),
                       verified=(r.get("selection") or {}).get("verified"))
            sf = stock_fp(r["footprint"], pr.dir)
            flags = []
            pkg = rec.get("package") or ""
            if sf and sf["th"] and _SMD_PKG.match(pkg.strip()):
                flags.append("through-hole footprint for an SMD package")
            if not _pkg_in_fp(pkg, r["footprint"]):
                flags.append("footprint name does not mention the package")
            row["flags"] = flags
        except ToolError as e:
            tried = (e.data or {}).get("tried") or []
            row.update(chosen="none", tried=[t["lib_id"] for t in tried], message=str(e.message)[:400],
                       rejected=[_reject_class("; ".join(t["notes"])) for t in tried if not t["ok"]])
        rows.append(row)
    L = []
    w = L.append
    w("== bundled offline table through the stock selection (prefer='stock'; supplier drawings: cache"
      + (" + network" if not os.environ.get("COPPER_OFFLINE") else " only") + ") ==")
    by: dict[str, list] = collections.defaultdict(list)
    for r in rows:
        by[r["kind"]].append(r)
    w(f"{'kind':<16} {'n':>5} {'stock':>6} {'(conv fp)':>9} {'rate':>6} {'flagged':>8} {'rejected':>9} {'no supplier data':>17}")
    nodata = lambda r: "supplier drawing unavailable" in str(r.get("message") or "")      # noqa: E731
    for k, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        st = [r for r in rs if r["chosen"] == "stock"]
        w(f"{k:<16} {len(rs):>5} {len(st):>6} {sum(1 for r in st if r.get('footprint_source') == 'converted'):>9} "
          f"{100 * len(st) / len(rs):>5.0f}% {sum(1 for r in st if r['flags']):>8} "
          f"{sum(1 for r in rs if r.get('rejected')):>9} {sum(1 for r in rs if nodata(r)):>17}")
    st = [r for r in rows if r["chosen"] == "stock"]
    w(f"{'TOTAL':<16} {len(rows):>5} {len(st):>6} {sum(1 for r in st if r.get('footprint_source') == 'converted'):>9} "
      f"{100 * len(st) / max(1, len(rows)):>5.0f}% {sum(1 for r in st if r['flags']):>8} "
      f"{sum(1 for r in rows if r.get('rejected')):>9} {sum(1 for r in rows if nodata(r)):>17}")
    w("")
    w("-- how the stock choices were made --")
    for k, n in collections.Counter(f"{r.get('match')} / footprint {r.get('footprint_source')} / {r.get('verified')}" for r in st).most_common():
        w(f"{n:>5}  {k}")
    w("")
    w("-- stock chosen, footprint does not fit the catalogue package (read these) --")
    for r in st:
        if r["flags"]:
            w(f"{r['lcsc']:<10} {str(r['mpn'])[:28]:<29} {str(r['package'])[:18]:<19} → {r['lib_id']} | {r['footprint']}   [{'; '.join(r['flags'])}]")
    w("")
    tv = [r for r in st if r["lib_id"] == "Device:D_TVS"]
    uni = collections.Counter(str(unidirectional(recs[r["lcsc"]])) for r in tv)
    w(f"-- TVS / ESD diodes placed as Device:D_TVS (the bidirectional symbol, pins A1/A2): {len(tv)} --")
    w(f"   unidirectional by catalogue text or MPN convention: {uni['True']}   bidirectional: {uni['False']}   undetermined: {uni['None']}")
    w("   e.g. " + "; ".join(f"{r['lcsc']} {r['mpn']}" for r in tv if unidirectional(recs[r["lcsc"]]))[:300])
    w("")
    w("-- stock candidates the verifier rejected, by reason --")
    c = collections.Counter(x for r in rows for x in r.get("rejected") or [])
    for k, n in c.most_common():
        w(f"{n:>4}  {k}")
    return rows, "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--json", default=str(OUT / "stock_coverage.json"))
    ap.add_argument("--table", action="store_true", help="sweep the whole bundled offline table instead of the corpus (no network)")
    ap.add_argument("--online", action="store_true", help="with --table: fetch missing supplier drawings (C-numbers only)")
    a = ap.parse_args(argv)
    setup(a.offline or (a.table and not a.online), a.refresh)
    from corpus import CORPUS
    from copper_kicad.kicad import env
    from copper_kicad.project import Project
    proj = WORK / "project"
    shutil.rmtree(proj, ignore_errors=True)
    proj.mkdir(parents=True)
    pr = Project(proj, "bench")
    if a.table:
        rows, text = table_sweep(pr)
        out = OUT / ("table_sweep_online.json" if a.online else "table_sweep.json")
        out.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
        out.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text)
        print(f"\n{len(rows)} parts; json: {out}")
        return 0
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()}
    rows = []
    for code, group, note in CORPUS:
        if only and code not in only:
            continue
        row = audit(code, group, note, pr)
        rows.append(row)
        print(f"  {code:<10} {row['chosen']:<10} {row.get('lib_id') or row.get('error', '')[:90]}", file=sys.stderr, flush=True)
    text = report(rows)
    inst = env.kicad()
    meta = {"kicad": inst.full_version or inst.version, "offline": a.offline, "parts": len(rows)}
    Path(a.json).parent.mkdir(parents=True, exist_ok=True)
    Path(a.json).write_text(json.dumps({"meta": meta, "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    (Path(a.json).with_suffix(".txt")).write_text(text + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    print(f"\nKiCad {meta['kicad']}; {len(rows)} parts; json: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
