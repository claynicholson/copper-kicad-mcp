"""Offline JLC Basic / Preferred parts table + free-text spec picker.

``pick('10k 0603')`` → the best JLC Basic part for a spec, instantly and
offline, from the bundled ``data/basic_parts.json.gz`` (built by
``parts.seed_build`` from the CDFER Basic/Preferred CSV plus curated common
Extended parts). Ranking: spec match → Basic > Preferred > Extended →
(0603 for unspecified chip passives) → stock → price.
"""
from __future__ import annotations

import functools
import math
import re
from pathlib import Path

from . import common as C

DATA = Path(__file__).resolve().parent.parent / "data" / "basic_parts.json.gz"

# words → kind
_KIND_WORDS = {
    "resistor": "resistor", "res": "resistor", "ohm": "resistor", "ohms": "resistor",
    "capacitor": "capacitor", "cap": "capacitor", "mlcc": "capacitor", "ceramic": "capacitor",
    "electrolytic": "electrolytic", "elec": "electrolytic", "ecap": "electrolytic", "aluminum": "electrolytic",
    "tantalum": "tantalum", "inductor": "inductor", "ind": "inductor", "choke": "inductor",
    "ferrite": "ferrite", "bead": "ferrite", "fb": "ferrite",
    "led": "led", "leds": "led",
    "diode": "diode", "rectifier": "diode", "schottky": "schottky", "zener": "zener", "tvs": "tvs", "esd": "esd",
    "mosfet": "mosfet", "nmos": "mosfet", "pmos": "mosfet", "fet": "mosfet",
    "npn": "bjt", "pnp": "bjt", "bjt": "bjt", "transistor": "bjt",
    "ldo": "ldo", "regulator": "ldo", "crystal": "crystal", "xtal": "crystal", "resonator": "crystal",
    "oscillator": "oscillator", "osc": "oscillator",
    "usb-c": "usb_connector", "usbc": "usb_connector", "type-c": "usb_connector", "typec": "usb_connector",
    "usb-a": "usb_connector", "usba": "usb_connector",
    "micro-usb": "usb_connector", "microusb": "usb_connector", "usb": "usb_connector",
    "header": "header", "pinheader": "header", "button": "switch", "switch": "switch", "tactile": "switch",
    "fuse": "fuse", "ptc": "fuse", "polyfuse": "fuse", "jst": "connector", "connector": "connector",
    "buck": "dcdc", "boost": "dcdc",
}
_FAMILIES = [
    {"diode", "schottky", "zener", "tvs", "esd", "rectifier", "bridge"},
    {"capacitor", "electrolytic", "tantalum"},
    {"inductor", "ferrite"},
    {"crystal", "oscillator"},
    {"connector", "header", "usb_connector"},
]
_PKG_RE = re.compile(
    r"^(?:(?:R|C|L|D|LED|FB)_?)?(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?:_\d{4}metric)?$|"
    r"^(sot-?23(?:-\d)?|sot-?223|sot-?89|sot-?323|sot-?363|sot-?563|sod-?123f?l?|sod-?323f?|sod-?523|sod-?923|"
    r"sma|smb|smc|smaf|do-?214a[abc]|so(?:ic|p)?-?\d+|tssop-?\d+|msop-?\d+|qfn-?\d+|dfn-?\d+|lqfp-?\d+|qfp-?\d+|"
    r"to-?252|to-?220|to-?263|to-?92|ssop-?\d+|hc-?49\w*|smd\d{4}(?:-\d+p)?|minimelf|melf|ll-?34|sod-?80)$", re.I)
_COLOR_WORDS = ("red", "green", "blue", "yellow", "white", "orange", "amber", "rgb", "pink", "purple", "warm-white",
                "infrared", "ir", "uv", "yellow-green", "emerald")
_SEMI_KINDS = {"diode", "schottky", "zener", "tvs", "esd", "mosfet", "bjt", "ldo", "dcdc"}
# SMD crystal body sizes (LCSC packages 'SMD3225-4P', 'SMD2520-4P', 'SMD5032-2P' …)
CRYSTAL_SIZES = ("1210", "1612", "2012", "2016", "2520", "3215", "3225", "5032", "6035", "7050")
_DIEL = {"x7r": "X7R", "x5r": "X5R", "c0g": "C0G", "np0": "C0G", "cog": "C0G", "y5v": "Y5V", "x6s": "X6S", "x7s": "X7S"}


def _alnum(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# ── data ─────────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _load() -> dict:
    try:
        return C.gz_json_load(DATA)
    except (OSError, ValueError):
        return {"meta": {"built": "missing", "count": 0}, "parts": []}


def snapshot_date() -> str:
    return _load()["meta"].get("built", "?")


def meta() -> dict:
    return dict(_load()["meta"])


def _expand(d: dict) -> dict:
    from .search import _rec
    r = _rec(lcsc=d["c"], mfr=d.get("m"), manufacturer=d.get("g"), package=d.get("p"), category=d.get("cat"),
             subcategory=d.get("sub"), description=d.get("d"), basic=d.get("b"), preferred=d.get("pr"),
             stock=d.get("s"), stock_source="snapshot " + snapshot_date(),
             price=[{"qty": q, "usd": u} for q, u in d.get("$") or []], datasheet=d.get("ds"),
             attributes=d.get("a") or {}, source="seed")  # kind/typed recomputed by _rec (stays in sync with classify)
    if d.get("cur"):
        r["curated"] = True
    return r


@functools.lru_cache(maxsize=1)
def _parts() -> tuple:
    return tuple(_expand(d) for d in _load()["parts"])


def all_parts() -> list[dict]:
    return list(_parts())


def by_lcsc(lcsc: str) -> dict | None:
    code = C.lcsc_norm(lcsc)
    for r in _parts():
        if r["lcsc"] == code:
            return dict(r)
    return None


# ── spec parsing ─────────────────────────────────────────────────────────
def parse_spec(text: str) -> dict:
    """Free-text spec → structured filter.

    '10k 0603' → {kind: resistor, value: 10000, package: '0603'}
    '100nF 16V 0402 X7R' → {kind: capacitor, value: 1e-7, voltage: 16, package: '0402', dielectric: 'X7R'}
    'LED red 0603' → {kind: led, color: red, package: '0603'}
    'AMS1117-3.3' → {mpn: 'AMS1117-3.3'};  'USB-C 16pin' → {kind: usb_connector, pins: 16}
    """
    spec: dict = {"raw": text, "keywords": []}
    s = str(text or "").replace("Ω", "Ω").replace("µ", "u").replace("μ", "u")
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)        # European decimal comma: '5,6K' / '4,7uF' (a separator otherwise)
    # '10 kOhm' / '4.7 kΩ' / '100 mΩ' / '100 nF' → one token; '10kOhm' → '10kΩ'
    s = re.sub(r"(\d)\s+([mkKM]?(?:ohms?|Ω)|[pnumkKM]?F|[pnum]?H|[kKM]?Hz|V|mA|A|W)(?![A-Za-z0-9])", r"\1\2", s, flags=re.I)
    s = re.sub(r"(\d)([mkKM]?)(?:ohms?|Ω)(?![A-Za-z0-9])", lambda m: m.group(1) + m.group(2) + "Ω", s, flags=re.I)
    # multi-word phrases → one token: 'USB A' / 'USB Type-A' / 'Type A' / 'USB-AF' → usb-a; 'USB type C' → usb-c;
    # 'pin header(s)' → pinheader (a male header in LCSC's 'Pin Headers' category); 'female header' → header female
    s = re.sub(r"(?i)(?<![\w-])(?:usb[\s_-]*)?type[\s_-]*a(?![\w-])|(?<![\w-])usb[\s_-]*a[fm]?(?![\w-])", "usb-a", s)
    s = re.sub(r"(?i)(?<![\w-])(?:usb[\s_-]*)?type[\s_]+c(?![\w-])|(?<![\w-])usb[\s_-]*type-c(?![\w-])|"
               r"(?<![\w-])usb[\s_]+c(?![\w-])", "usb-c", s)
    s = re.sub(r"(?i)(?<![\w-])pin[\s_-]*headers?(?![\w-])", "pinheader", s)
    s = re.sub(r"(?i)(?<![\w-])female[\s_-]*headers?(?![\w-])", "header female", s)
    toks = []
    for t in re.split(r"[\s,;/]+", s):
        # 'RED-LED' / 'GREEN_LED' (schematic Values) → 'red', 'led' when both halves are known words
        parts = [x for x in re.split(r"[-_]", t) if x]
        if len(parts) > 1 and t.lower() not in _KIND_WORDS and t.lower() not in _COLOR_WORDS \
                and all(x.lower() in _KIND_WORDS or x.lower() in _COLOR_WORDS for x in parts):
            toks += parts
        elif t:
            toks.append(t)
    pin_word = False
    for raw in toks:
        t = raw.strip("()[]")
        tl = t.lower()
        if not t:
            continue
        # footprint-ish tokens: R_0603_1608Metric, Resistor_SMD:R_0402_1005Metric
        if ":" in t:
            t = t.split(":", 1)[1]
            tl = t.lower()
        if _PKG_RE.match(tl):
            spec["package"] = C.norm_pkg(t)
            pre = re.match(r"^(r|c|l|d|led|fb)_", tl)
            if pre and "kind" not in spec:
                spec["kind"] = {"r": "resistor", "c": "capacitor", "l": "inductor", "d": "diode", "led": "led",
                                "fb": "ferrite"}[pre.group(1)]
            continue
        if tl in _DIEL:
            spec["dielectric"] = _DIEL[tl]
            continue
        m = re.fullmatch(r"[±+]?(\d+(?:\.\d+)?)%", tl)
        if m:
            spec["tolerance"] = float(m.group(1)) / 100
            continue
        m = re.fullmatch(r"(\d+)(?:p|pin|pins|pos|position|-?pin)", tl)
        if m and int(m.group(1)) < 400:
            spec["pins"] = int(m.group(1))
            continue
        m = re.fullmatch(r"(\d+)[x*×](\d+)(?:p|pin|pins)?", tl)
        if m:
            spec["pins"] = int(m.group(1)) * int(m.group(2))
            spec["rows"] = int(m.group(1))
            continue
        if tl in ("female", "socket", "sockets", "receptacle"):
            spec["gender"] = "female"
            continue
        if tl in ("male", "plug"):
            spec["gender"] = "male"
            continue
        if tl in ("pin", "pins"):
            pin_word = True
            continue
        if tl in ("n-channel", "nch", "n-ch", "nmos", "n-mos"):
            spec["channel"] = "N"; spec.setdefault("kind", "mosfet"); continue
        if tl in ("p-channel", "pch", "p-ch", "pmos", "p-mos"):
            spec["channel"] = "P"; spec.setdefault("kind", "mosfet"); continue
        if tl in ("npn", "pnp"):
            spec["polarity"] = tl.upper(); spec.setdefault("kind", "bjt"); continue
        col = C.led_color(tl) if tl in _COLOR_WORDS else None
        if col or tl == "ir":
            spec["color"] = col or "infrared"
            continue
        if tl in _KIND_WORDS:
            k = _KIND_WORDS[tl]
            if k == "usb_connector" and tl == "usb":
                spec.setdefault("kind", k)
            else:
                # a more specific kind beats a generic one (schottky > diode)
                if "kind" not in spec or spec["kind"] in ("diode", "capacitor", "bjt") and k != spec["kind"] \
                        or (k == "ferrite" and spec["kind"] == "resistor"):
                    spec["kind"] = k
            if tl in ("micro-usb", "microusb"):
                spec["usb"] = "micro"
            if tl in ("usb-c", "usbc", "type-c", "typec"):
                spec["usb"] = "c"
            if tl in ("usb-a", "usba"):
                spec["usb"] = "a"
            if tl == "pinheader":
                pin_word = True
            continue
        # values with units
        v = None
        for unit, kind in (("ohm", "resistor"), ("F", "capacitor"), ("H", "inductor"), ("Hz", "crystal")):
            if re.search({"ohm": r"(ohms?|Ω|\dR\d*$|^\d+(?:\.\d+)?[mkKM]?R$)", "F": r"\dF$|[pnu]F$", "H": r"[pnum]?H$",
                          "Hz": r"Hz$"}[unit], t, re.I) and not (unit == "H" and tl.endswith("hz")):
                v = C.parse_si(t, unit)
                if v is not None:
                    spec["value"], spec["unit"] = v, unit
                    if spec.get("kind") not in (kind,) and not (kind == "capacitor" and spec.get("kind") in ("electrolytic", "tantalum")) \
                            and not (kind == "inductor" and spec.get("kind") == "ferrite") \
                            and not (kind == "crystal" and spec.get("kind") == "oscillator"):
                        spec["kind"] = kind if spec.get("kind") in (None, "resistor", "capacitor", "inductor") else spec["kind"]
                    break
        if v is not None:
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(m?)v", tl)
        if m:
            spec["voltage"] = float(m.group(1)) * (1e-3 if m.group(2) else 1)
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(m?)a", tl)
        if m:
            spec["current"] = float(m.group(1)) * (1e-3 if m.group(2) else 1)
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?|\d+/\d+)(m?)w", tl)
        if m:
            spec["power"] = C.parse_si(t, "W")
            continue
        # bare value: 10k, 4k7, 1M, 100R → resistor; 100n / 4u7 / 22p → capacitor (inductor if kind says so)
        if re.fullmatch(r"\d+(?:\.\d+)?[kKM]|\d+[kKMR]\d+|\d+(?:\.\d+)?", t):
            v = C.parse_si(t, "ohm")
            if v is not None and (re.search(r"[kKMR]", t) or spec.get("kind") == "resistor"):
                spec["value"], spec["unit"] = v, "ohm"
                spec.setdefault("kind", "resistor")
                continue
            if v is not None and "bare" not in spec:
                # a unit-less number ('470', '47') is ohms by schematic convention, decided after
                # all tokens are read (a later kind word may change it). It must never become a
                # keyword: '470' substring-matches '0402WGF4701TCE' (a 4.7k part).
                spec["bare"] = v
                spec["_bare_tok"] = t
                continue
        if re.fullmatch(r"\d+(?:\.\d+)?[pnu]|\d+[pnu]\d+", t):
            unit = "H" if spec.get("kind") in ("inductor", "ferrite") else "F"
            v = C.parse_si(t, unit)
            if v is not None:
                spec["value"], spec["unit"] = v, unit
                spec.setdefault("kind", "capacitor" if unit == "F" else "inductor")
                continue
        m = re.fullmatch(r"(?:p=?)?(\d+(?:\.\d+)?)mm", tl)
        if m:
            spec["pitch"] = float(m.group(1))
            continue
        # MPN-like: letters+digits, ≥3 chars
        if re.search(r"[a-z]", tl) and re.search(r"\d", tl) and len(tl) >= 3 and not spec.get("mpn"):
            spec["mpn"] = t
            continue
        spec["keywords"].append(tl)
    bare = spec.pop("bare", None)
    bare_tok = spec.pop("_bare_tok", None)
    if bare is not None and spec.get("kind") in ("crystal", "oscillator") and bare_tok in CRYSTAL_SIZES \
            and not spec.get("package"):
        spec["package"] = f"SMD{bare_tok}"          # '8MHz crystal 3225' → SMD3225-4P parts
        bare = None
    if bare is not None and spec.get("value") is None and not spec.get("mpn") and re.fullmatch(r"\d{3,}", bare_tok or "") \
            and (spec.get("kind") in _SEMI_KINDS
                 or (spec.get("kind") is None and spec.get("package") and spec["package"] not in C.CHIP_SIZES)):
        # '7805 TO-220' / 'LM317 regulator' style: a bare part number on a semiconductor package (or with
        # a non-passive kind word) is an MPN, never a 7805 Ω resistor
        spec["mpn"] = bare_tok
        bare = None
    if bare is not None and spec.get("value") is None:
        if spec.get("kind") in (None, "resistor") and not spec.get("mpn"):
            spec["value"], spec["unit"], spec["kind"] = bare, "ohm", "resistor"
        elif spec.get("kind") == "ferrite":
            spec["value"], spec["unit"] = bare, "ohm"
        else:
            spec["keywords"].append(f"{bare:g}")
    if spec.get("kind") in ("zener", "tvs", "esd") and spec.get("voltage") is not None:
        spec["vz"] = spec.pop("voltage")
    if spec.get("kind") == "ldo" and spec.get("voltage") and not spec.get("value"):
        spec["vout"] = spec.pop("voltage")
    if spec.get("kind") == "resistor" and spec.get("unit") not in (None, "ohm"):
        spec.pop("kind")
    if pin_word and spec.get("kind") == "header":
        spec.setdefault("gender", "male")             # 'pin header' = male pins (LCSC 'Pin Headers')
    # a bare "16P"/"16pin" with USB etc stays; a lone mpn decides kind at match time
    return spec


def pkg_match(record_pkg: str | None, want: str) -> bool:
    """Package equality after normalisation; chip sizes must match exactly
    ('0603' ≠ '0603x4'), others may match as a whole token ('SOT-23-6' in
    'SOT-23-6L')."""
    sp = C.norm_pkg(want)
    rp = C.norm_pkg(record_pkg)
    if rp == sp:
        return True
    if sp in C.CHIP_SIZES:
        return False
    raw = (record_pkg or "").upper().replace("_", "-")
    if re.fullmatch(r"\d{4}", sp):
        # a bare body size ('3225', '2520') matches 'SMD3225-4P' / 'SMD-3225' crystals and oscillators
        return bool(re.search(rf"(?:^|SMD-?|[^0-9A-Z]){sp}(?![0-9])", raw))
    m = re.fullmatch(r"SMD(\d{4})", sp)
    if m:
        return bool(re.search(rf"(?:^|SMD-?|[^0-9A-Z]){m.group(1)}(?![0-9])", raw))
    return bool(re.search(rf"(?<![A-Z0-9]){re.escape(sp)}(?![0-9A-Z])", raw))


def _same_family(a: str, b: str) -> bool:
    if a == b:
        return True
    return any(a in f and b in f for f in _FAMILIES)


def spec_matches(r: dict, spec: dict) -> int:
    """0 strong, 1 partial, 2 keyword-only, 9 = contradicts the spec."""
    score = 0
    typed = r.get("typed") or {}
    rk = r.get("kind") or ""
    text = f"{r.get('mfr','')} {r.get('description','')} {r.get('package','')} {r.get('subcategory','')}".lower()
    if spec.get("mpn"):
        q, m = _alnum(spec["mpn"]), _alnum(r.get("mfr"))
        if m == q or (m.startswith(q) and len(m) - len(q) <= 4) or (q.startswith(m) and len(m) >= 4 and len(q) - len(m) <= 3):
            pass
        elif mpn_family(q, m):
            pass        # '7805' → L7805CV / MC7805CT: a bare numeric part number with a maker prefix
        elif q in m:
            score = max(score, 1)
        elif q in _alnum(text):
            score = max(score, 2)
        else:
            return 9
    sk = spec.get("kind")
    if sk:
        if not _same_family(sk, rk):
            # generic 'usb' / 'connector' queries may match headers etc. only weakly
            return 9
        if sk in ("header", "usb_connector") and rk != sk \
                and not (sk == "header" and rk == "connector" and "header" in text):
            return 9      # a 'Header 2x10' query never returns a USB-C receptacle (and vice versa)
        if sk != rk and sk not in ("diode", "capacitor", "connector"):
            score = max(score, 1)
        if sk == "capacitor" and rk in ("electrolytic", "tantalum"):
            if (spec.get("value") or 0) < 10e-6:
                return 9
            score = max(score, 1)
    if spec.get("value") is not None:
        unit = spec.get("unit")
        key = {"ohm": "ohm", "F": "F", "H": "H", "Hz": "Hz"}.get(unit)
        have = typed.get(key) if key else None
        if have is None and rk == "ferrite" and unit == "ohm":
            have = typed.get("ohm")
        if have is None:
            # try description
            for mm in re.finditer(r"(\d+(?:\.\d+)?)\s*([pnuµμmkKMG]?)\s*(Ω|F|H|Hz)\b", r.get("description") or ""):
                u = {"Ω": "ohm"}.get(mm.group(3), mm.group(3))
                if u == unit:
                    have = float(mm.group(1)) * C._PREFIX.get(mm.group(2), 1.0)
                    break
        if have is None:
            score = max(score, 2)
        elif not C.close(have, spec["value"], 0.006):
            return 9
    if spec.get("package"):
        rp = C.norm_pkg(r.get("package"))
        sp = spec["package"]
        if not pkg_match(r.get("package"), sp):
            return 9
    if spec.get("voltage") is not None and rk in ("capacitor", "electrolytic", "tantalum"):
        v = typed.get("V")
        if v is None:
            score = max(score, 1)
        elif v + 1e-9 < spec["voltage"]:
            return 9
    if spec.get("vz") is not None:
        if rk not in ("zener", "tvs", "esd"):
            return 9
        # zener: nominal Vz; TVS/ESD: reverse stand-off — both must be the asked voltage
        v = typed.get("V")
        if v is None:
            vs = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*V\b", r.get("description") or "")]
            v = vs[0] if vs else None
            score = max(score, 1)
        if v is None or not C.close(v, spec["vz"], 0.08):
            return 9
    if spec.get("pitch"):
        p = f"{spec['pitch']:g}"
        if not re.search(rf"(?<![\d.]){re.escape(p)}0*\s*mm|p\s*=?\s*{re.escape(p)}(?![\d])", text):
            score = max(score, 1)
    if spec.get("vout") is not None:
        v = typed.get("Vout")
        if v is None:
            m = re.search(r"-(\d)\.?(\d)", r.get("mfr") or "")
            v = float(f"{m.group(1)}.{m.group(2)}") if m else None
        if v is None or not C.close(v, spec["vout"], 0.02):
            return 9
    if spec.get("tolerance") is not None:
        tol = typed.get("tol")
        if tol is None:
            score = max(score, 1)
        elif tol > spec["tolerance"] + 1e-9:
            return 9
    if spec.get("dielectric"):
        d = (typed.get("diel") or "").upper()
        if not d:
            score = max(score, 1)
        elif spec["dielectric"] not in d:
            return 9
    if spec.get("color"):
        c = typed.get("color") or C.led_color(text)
        if c is None or c != spec["color"]:
            return 9
    if spec.get("channel"):
        ch = typed.get("channel")
        if ch and ch != spec["channel"]:
            return 9
        if not ch:
            score = max(score, 1)
    if spec.get("polarity"):
        p = typed.get("polarity") or ("PNP" if "pnp" in text else ("NPN" if "npn" in text else None))
        if p and p != spec["polarity"]:
            return 9
    if spec.get("usb") == "c" and not re.search(r"type-?c|usb-?c", text):
        return 9
    if spec.get("usb") == "micro" and "micro" not in text:
        return 9
    if spec.get("usb") == "a":
        full = text + " " + (r.get("mfr") or "").lower()
        if re.search(r"type-?c|usb-?c\b|micro|mini", full) or \
                not re.search(r"type[-\s]?a(?![a-z])|usb[-\s]?a(?![a-z])|usb[-\s]?a[fm]\b|(?<![a-z])a[fm](?![a-z])", full):
            return 9
    g = spec.get("gender")
    if g or sk in ("header", "usb_connector"):
        fem = bool(re.search(r"female|socket|receptacle", text))
        if g == "male" and fem:
            return 9
        if g == "female" and not fem:
            if rk == "header" or "pin header" in text:
                return 9      # LCSC files female headers under 'Female Headers'
            score = max(score, 1)
        if g is None and sk == "header" and fem:
            score = max(score, 1)    # 'Header 1x5' means pins unless 'female' is asked
    if spec.get("pins"):
        n = spec["pins"]
        if not re.search(rf"(?<!\d){n}\s*(p\b|pin|pins|pos|-pin|p\)|p,|position)|\b1x{n}p?\b|{n}p(?![a-z0-9])", text):
            score = max(score, 1)
    if spec.get("rows"):
        full = text + " " + (r.get("mfr") or "").lower()
        m = re.search(r"(?<![\d.])(\d)\s*[x*×]\s*(\d+)\s*p?", full)
        if m and (int(m.group(1)) != spec["rows"] or
                  (spec.get("pins") and int(m.group(1)) * int(m.group(2)) != spec["pins"])):
            return 9   # 2x5 is not 1x10, 2x3 is not 2x5
        if not m and spec["rows"] == 1 and re.search(r"double|dual row|2 rows", full):
            return 9
    if sk in ("header", "connector") and not re.search(r"right.?angle|bend|90°|90 deg|r/a", (spec.get("raw") or "").lower()):
        if re.search(r"right.?angle|bend|r/a|90°", text + " " + (r.get("mfr") or "").lower()):
            score = max(score, 1)   # prefer straight unless asked
    if spec.get("current") is not None and rk not in ("resistor", "capacitor", "electrolytic", "tantalum", "led"):
        have = typed.get("A")
        if have is None:
            cs = C.desc_currents(r.get("description") or "")
            # inductors list rated + saturation current: usable = the lower; elsewhere the rating is the largest
            have = (min(cs) if rk in ("inductor", "ferrite") else max(cs)) if cs else None
        if have is None:
            score = max(score, 1)          # current not confirmed → 'partial', never silently 'exact'
        elif have + 1e-9 < spec["current"]:
            return 9
    kws = [k for k in spec.get("keywords", []) if len(k) > 1 and k not in ("smd", "smt", "part", "the", "a", "for")]
    if kws:
        hits = sum(1 for k in kws if k in text)
        if hits == 0 and not (sk or spec.get("mpn") or spec.get("value") is not None):
            return 9
        if hits < len(kws):
            score = max(score, 1 if hits else 2)
    if not (sk or spec.get("mpn") or spec.get("value") is not None or kws or spec.get("package")):
        return 2
    return score


# ── pick ─────────────────────────────────────────────────────────────────
_PKG_PREF = {"0603": 0, "0402": 1, "0805": 1, "1206": 2}


def candidates(spec: dict | str, limit: int | None = None) -> list[dict]:
    if isinstance(spec, str):
        spec = parse_spec(spec)
    out = []
    for r in _parts():
        s = spec_matches(r, spec)
        if s < 9:
            rr = dict(r)
            rr["_score"] = s
            out.append(rr)
    out.sort(key=lambda r: _rank(r, spec))
    return out[:limit] if limit else out


def _rank(r: dict, spec: dict):
    """The one ranking used by parts_search, spec placement (pick) and BOM sourcing:
    spec relevance → exact MPN → Basic > Preferred > Extended → package/voltage/current
    sanity → enough stock → price → stock."""
    stock = r.get("stock") or 0
    unit = C.unit_price(r.get("price") or [], 100)
    typed = r.get("typed") or {}
    exact = 0
    if spec.get("mpn"):
        exact = 0 if _alnum(r.get("mfr")) == _alnum(spec["mpn"]) else 1
    pkg_pref = 0
    if not spec.get("package") and r.get("kind") in ("resistor", "capacitor", "led", "ferrite"):
        pkg_pref = _PKG_PREF.get(C.norm_pkg(r.get("package")), 3)
    # capacitors: when no voltage asked, don't prefer 6.3V over 16/25/50V parts
    vpen = 0
    if r.get("kind") == "capacitor" and not spec.get("voltage"):
        v = typed.get("V") or 0
        vpen = 0 if v >= 16 else 1
    # inductors: with no current/package asked, a 5 mA multilayer chip is never the best default for '22uH'
    ipen = 0
    if r.get("kind") == "inductor" and spec.get("current") is None and not spec.get("package"):
        a = typed.get("A")
        ipen = 1 if a is not None and a < 0.1 else 0
    # headers: 2.54 mm unless another pitch is asked
    ppen = 0
    if r.get("kind") == "header" and not spec.get("pitch"):
        ppen = 0 if re.search(r"2\.54", f"{r.get('package')} {r.get('description')}") else 1
    # stock is a decade tier, not a sort key: snapshot vs live stock must not reorder two parts with
    # similar stock (then price decides), but a 300k-stock connector still beats a 19k-stock one
    tier = 6 - min(6, int(math.log10(stock))) if stock > 0 else 9
    return (r.get("_score", 0), ipen, ppen, C.CLASS_RANK.get(r.get("class"), 3), tier, exact, pkg_pref, vpen,
            unit if unit is not None else 9e9, -stock)


def mpn_family(q: str, m: str) -> bool:
    """Generic part number (starts with a digit) vs a maker-prefixed MPN: '7805' ~ 'l7805cv' / 'mc7805ctg',
    '78l05' ~ 'l78l05acz' (alnum inputs, any case). Never '7805' ~ '78051'."""
    q, m = (q or "").lower(), (m or "").lower()
    if not q or not q[0].isdigit() or len(q) < (3 if q.isdigit() else 4):
        return False
    return bool(re.fullmatch(rf"[a-z]{{0,3}}{re.escape(q)}(?:[a-z][a-z0-9]{{0,7}})?", m))


def canonical_value(r: dict) -> str:
    t = r.get("typed") or {}
    k = r.get("kind")
    if k == "resistor" and t.get("ohm") is not None:
        return C.fmt_si(t["ohm"], "ohm")
    if k in ("capacitor", "electrolytic", "tantalum") and t.get("F") is not None:
        return C.fmt_si(t["F"], "F")
    if k == "inductor" and t.get("H") is not None:
        return C.fmt_si(t["H"], "H")
    if k in ("crystal", "oscillator") and t.get("Hz") is not None:
        return C.fmt_si(t["Hz"], "Hz")
    if k == "led" and t.get("color"):
        return f"LED {t['color']}"
    if k == "ferrite" and t.get("ohm") is not None:
        return f"{C.fmt_si(t['ohm'], 'ohm')}@100MHz"
    return r.get("mfr") or ""


def _picked(best: dict, sp: dict, alts: list[dict], source: str) -> dict:
    out = {
        "lcsc": best["lcsc"], "value": canonical_value(best), "package": best.get("package"),
        "description": best.get("description"), "mfr": best.get("mfr"), "manufacturer": best.get("manufacturer"),
        "class": best.get("class"), "basic": best.get("basic"), "preferred": best.get("preferred"),
        "stock": best.get("stock"), "stock_source": best.get("stock_source"), "kind": best.get("kind"),
        "price": best.get("price"), "datasheet": best.get("datasheet"),
        "match": "exact" if best.get("_score", 0) == 0 else "partial",
        "spec": {k: v for k, v in sp.items() if v not in (None, [], "") and not k.startswith("_")},
        "alternatives": [c["lcsc"] for c in alts[:3]], "source": source,
    }
    if best.get("class") != "Basic":
        w = f"{best['lcsc']} is JLC {best.get('class')} (no Basic part matches '{sp.get('raw')}'"
        if best.get("class") == "Extended":
            w += f"; ≈${C.JLC_SETUP_FEE_USD:.0f} setup fee per order"
        alt = _basic_elsewhere(sp)
        if alt:
            w += f"; Basic {alt['lcsc']} is {alt.get('package')} — use '{_respec(sp, alt)}' if that package fits"
        out["warning"] = w + ")"
    return out


def _basic_elsewhere(sp: dict) -> dict | None:
    """A Basic part that matches the spec except for its package (green LED: 0805 Basic, 0603 Extended)."""
    if not sp.get("package"):
        return None
    loose = {k: v for k, v in sp.items() if k != "package"}
    for c in candidates(loose):
        if c["_score"] == 0 and c.get("basic") and (c.get("stock") or 0) > 0:
            return c
    return None


def _respec(sp: dict, alt: dict) -> str:
    raw = str(sp.get("raw") or "")
    new = C.norm_pkg(alt.get("package"))
    if sp.get("package") and str(sp["package"]).lower() in raw.lower():
        return re.sub(re.escape(str(sp["package"])), new, raw, flags=re.I)
    return f"{raw} {new}"


def pick(spec: str, allow_partial: bool = True, live: bool = True) -> dict | None:
    """'10k 0603' / '100nF 0402 16V' / 'LED red 0603' / 'AMS1117-3.3' → best JLC part, Basic first.

    The offline Basic/Preferred table answers instantly; when it has no acceptable match ('27R 0402':
    no Basic 27 Ω exists) the live JLC search is used with the same ranking as parts_search, so spec
    placement never silently drops to a generic symbol while an in-stock LCSC part exists. A non-Basic
    pick carries a 'warning'. None only when nothing in stock matches."""
    sp = parse_spec(spec)
    if not (sp.get("kind") or sp.get("mpn") or sp.get("value") is not None or sp.get("keywords")):
        return None
    lim = 1 if allow_partial else 0
    cands = [c for c in candidates(sp) if (c.get("stock") or 0) > 0 or c.get("stock") is None]
    off = [c for c in cands if c["_score"] <= lim]
    if off and (off[0].get("basic") or off[0].get("preferred") or not live):
        return _picked(off[0], sp, off[1:], "offline table")
    if live:
        from . import search
        try:
            res = search.search(spec, {"in_stock": True}, limit=6, live=True, raw=True)
        except C.NetError:
            res = None
        recs = [r for r in (res or {}).get("records", []) if r.get("_score", 9) <= lim]
        pool = recs + off
        if pool:
            pool.sort(key=lambda r: _rank(r, sp))
            seen, uniq = set(), []
            for r in pool:
                if r["lcsc"] not in seen:
                    seen.add(r["lcsc"])
                    uniq.append(r)
            return _picked(uniq[0], sp, uniq[1:], "live JLC search" if uniq[0].get("source") == "jlc" else "offline table")
    if off:
        return _picked(off[0], sp, off[1:], "offline table")
    return None
