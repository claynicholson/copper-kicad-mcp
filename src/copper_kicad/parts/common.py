"""Shared plumbing for the parts engine: HTTP with retry/backoff + per-host
rate limiting, a JSON disk cache with TTL, SI value parsing/formatting,
package normalisation and part classification.

Standard library only.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import __version__

# Browser UA — EasyEDA's CloudFront blocks short / non-browser agents.
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
# Honest UA for the JLC / jlcsearch APIs.
UA_HONEST = f"copper-kicad/{__version__} (KiCad MCP parts engine; low-rate, cached)"

JLC_SETUP_FEE_USD = 3.0   # per unique Extended part per order (JLC "feeder loading fee")


class NetError(Exception):
    """Network/source failure (offline, blocked, overloaded, bad payload)."""


# ── cache ────────────────────────────────────────────────────────────────
def home() -> Path:
    return Path(os.environ.get("COPPER_HOME", Path.home() / ".copper"))


def cache_dir(ns: str = "") -> Path:
    d = home() / "cache" / "parts"
    if ns:
        d = d / ns
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:60]
    return f"{safe}-{hashlib.sha1(key.encode()).hexdigest()[:10]}.json"


def cache_get(ns: str, key: str, ttl: float | None):
    """Return cached object or None. ttl=None → never expires."""
    f = cache_dir(ns) / _key(key)
    try:
        if ttl is not None and time.time() - f.stat().st_mtime > ttl:
            return None
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cache_age(ns: str, key: str) -> float | None:
    try:
        return time.time() - (cache_dir(ns) / _key(key)).stat().st_mtime
    except OSError:
        return None


def cache_put(ns: str, key: str, obj):
    f = cache_dir(ns) / _key(key)
    tmp = f.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass


# ── HTTP ─────────────────────────────────────────────────────────────────
_host_lock = threading.Lock()
_host_next: dict[str, float] = {}
MIN_INTERVAL = {"jlcpcb.com": 1.05, "jlcsearch.tscircuit.com": 0.35, "wmsc.lcsc.com": 1.0}


def offline() -> bool:
    return os.environ.get("COPPER_OFFLINE", "").lower() in ("1", "true", "yes")


def _throttle(url: str):
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url).lower()
    gap = next((v for k, v in MIN_INTERVAL.items() if host.endswith(k)), 0.0)
    if not gap:
        return
    with _host_lock:
        now = time.time()
        wait = _host_next.get(host, 0.0) - now
        _host_next[host] = max(now, _host_next.get(host, 0.0)) + gap
    if wait > 0:
        time.sleep(wait)


def http(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: float = 25,
         retries: int = 3, backoff: float = 1.5, retry_if=None) -> bytes:
    """GET/POST with retry + exponential backoff on 429/5xx/timeouts and on
    bodies ``retry_if(body)`` flags (e.g. jlcsearch 'D1 overloaded')."""
    if offline():
        raise NetError("offline mode (COPPER_OFFLINE=1)")
    h = {"User-Agent": UA_HONEST, "Accept": "application/json, */*"}
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        _throttle(url)
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
            if retry_if and retry_if(body):
                last = NetError(f"{url}: transient upstream error: {body[:160]!r}")
            else:
                return body
        except urllib.error.HTTPError as e:
            last = NetError(f"{url}: HTTP {e.code}")
            if e.code not in (408, 425, 429, 500, 502, 503, 504):
                raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = NetError(f"{url}: {getattr(e, 'reason', e)}")
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt) * (0.75 + 0.5 * (hash(url) % 100) / 100))
    raise last or NetError(url)


def http_json(url: str, payload: dict | None = None, **kw):
    data = None
    headers = dict(kw.pop("headers", {}) or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    body = http(url, data=data, headers=headers, **kw)
    try:
        return json.loads(body)
    except ValueError:
        raise NetError(f"{url}: non-JSON response {body[:120]!r}")


# ── identifiers ──────────────────────────────────────────────────────────
def lcsc_norm(x) -> str | None:
    """'25804' / 'c25804' / 'C25804' / 'https://…_C25804.html' → 'C25804'."""
    if x is None:
        return None
    s = str(x).strip()
    m = re.fullmatch(r"(?:LCSC[\s#:]*)?[Cc]?[\s-]?(\d{1,10})", s, re.I)
    if m:
        return "C" + m.group(1)
    m = re.search(r"(?<![A-Za-z0-9])C(\d{2,10})(?![0-9])", s)
    return "C" + m.group(1) if m else None


def jlc_class(basic: bool, preferred: bool) -> str:
    return "Basic" if basic else ("Preferred" if preferred else "Extended")


CLASS_RANK = {"Basic": 0, "Preferred": 1, "Extended": 2}


def price_ladder(s) -> list[dict]:
    """'1-49:0.0887,50-149:0.0707,6000-:0.0451' or JLC componentPrices list → [{qty, usd}]."""
    out = []
    if isinstance(s, list):
        for p in s:
            try:
                out.append({"qty": int(p.get("startNumber") or p.get("qty") or 1),
                            "usd": float(p.get("productPrice") if p.get("productPrice") is not None else p.get("usd"))})
            except (TypeError, ValueError):
                pass
    elif isinstance(s, str):
        for part in s.split(","):
            m = re.match(r"\s*(\d+)\s*-\s*(\d*)\s*:\s*([\d.]+)", part)
            if m:
                out.append({"qty": int(m.group(1)), "usd": float(m.group(3))})
    out.sort(key=lambda p: p["qty"])
    return out


def ladder_str(ladder: list[dict], n: int = 5) -> str:
    """Compact price ladder: '1+ $0.0022 · 1k+ $0.0018 · 10k+ $0.0015'."""
    def q(v):
        return f"{v // 1000}k" if v >= 1000 and v % 1000 == 0 else str(v)
    return " · ".join(f"{q(p['qty'])}+ ${p['usd']:g}" for p in (ladder or [])[:n])


def unit_price(ladder: list[dict], qty: int = 10) -> float | None:
    best = None
    for p in ladder or []:
        if p["qty"] <= qty:
            best = p["usd"]
    if best is None and ladder:
        best = ladder[0]["usd"]
    return best


# ── SI values ────────────────────────────────────────────────────────────
_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
           "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0, "r": 1.0}
_UNIT_ALIASES = {
    "ohm": ("Ω", "ohm", "ohms", "Ohm", "R", "r", "ω"),
    "F": ("F", "f"),
    "H": ("H", "h"),
    "V": ("V", "v"),
    "A": ("A", "a"),
    "W": ("W", "w"),
    "Hz": ("Hz", "HZ", "hz"),
}


def parse_si(text: str, unit: str | None = None) -> float | None:
    """Parse '10k', '4k7', '4.7uF', '100nF', '0R', '1M', '2.2 µH', '32.768kHz',
    '500mA', '1/4W' into a float (SI base unit). ``unit`` restricts the unit
    suffix accepted ('ohm','F','H','V','A','W','Hz'); with unit='ohm' a bare
    number or R/k/M suffix is accepted."""
    if text is None:
        return None
    s = str(text).strip().replace(",", ".").replace(" ", "").replace("Ω", "ohm").replace("Ω", "ohm")
    if not s:
        return None
    m = re.fullmatch(r"(\d+)/(\d+)([Ww])", s)
    if m and unit in (None, "W"):
        return int(m.group(1)) / int(m.group(2))
    units = _UNIT_ALIASES.get(unit, ()) if unit else sum(_UNIT_ALIASES.values(), ())
    # 4k7 / 4R7 / 2u2 / 4n7 style
    m = re.fullmatch(r"(\d+)([pnuµμmkKMGRr])(\d+)(ohms?|Ohm|[FfHhVv]|Hz)?", s)
    if m:
        u = m.group(4) or ""
        if unit == "ohm" and u.lower() not in ("", "ohm", "ohms") or (u and unit and unit != "ohm" and u.lower() != unit.lower()):
            return None
        v = float(f"{m.group(1)}.{m.group(3)}") * _PREFIX[m.group(2)]
        return v
    m = re.fullmatch(r"([-+]?\d+(?:\.\d+)?|\.\d+)([pnuµμmkKMG]?)(ohms?|Ohm|[RrFfHhVvAaWw]|Hz|HZ|hz)?", s)
    if not m:
        return None
    num, pre, u = float(m.group(1)), m.group(2), m.group(3) or ""
    if u in ("R", "r"):
        if unit not in (None, "ohm"):
            return None
    elif u:
        if unit and u.lower().rstrip("s") not in [a.lower() for a in units]:
            return None
    # 'm' with ohm/F means milli; 'M' means mega
    return num * _PREFIX.get(pre, 1.0)


def fmt_si(v: float | None, unit: str = "") -> str:
    if v is None:
        return ""
    if v == 0:
        return "0" + ("Ω" if unit == "ohm" else unit)
    for p, mul in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(v) >= mul * 0.9999:
            x = v / mul
            s = (f"{x:.3f}").rstrip("0").rstrip(".")
            if unit == "ohm":
                return s + (p if p else "")  # '10k', '4.7k', '100'
            return f"{s}{p}{unit}"
    return f"{v:g}{unit}"


def close(a: float | None, b: float | None, rel: float = 0.005) -> bool:
    if a is None or b is None:
        return False
    if a == b:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b))


# ── packages ─────────────────────────────────────────────────────────────
CHIP_SIZES = ["01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512"]
_METRIC = {"0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216", "1210": "3225", "0201": "0603",
           "2512": "6332", "2010": "5025", "1812": "4532"}
_METRIC_INV = {v: k for k, v in _METRIC.items()}


def norm_pkg(p: str | None) -> str:
    """Normalise a package name for comparison: 'SOT-23-3L'→'SOT-23',
    'SMA(DO-214AC)'→'SMA', 'LQFP-48(7x7)'→'LQFP-48', 'SO-8'/'SOP-8'→'SOIC-8',
    'R_0603_1608Metric'→'0603', 'C0603'→'0603'."""
    if not p:
        return ""
    s = str(p).upper().strip()
    m = re.search(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?:_(\d{4})METRIC)?(?!\d)", s)
    if m and ("METRIC" in s or re.fullmatch(r"(?:[RCLD]|LED|FB)?_?\d{4,5}", s) or
              re.match(r"(R|C|L|LED|D|FB)_\d{4}", s)):
        return m.group(1)
    m = re.fullmatch(r"(\d{4})METRIC", s)
    if m and m.group(1) in _METRIC_INV:
        return _METRIC_INV[m.group(1)]
    s = s.replace("_", "-").replace(" ", "")
    if s.startswith("SMA") and ("DO-214AC" in s or s == "SMA"):
        return "SMA"
    if "DO-214AC" in s:
        return "SMA"
    if s.startswith("SMB") and "SMBF" not in s or "DO-214AA" in s:
        return "SMB"
    if s.startswith("SMC") or "DO-214AB" in s:
        return "SMC"
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"^(SOT-?23)-3L?$", "SOT-23", s)
    s = re.sub(r"^SOT-?23$", "SOT-23", s)
    s = re.sub(r"^SOT-?23-(\d)L?$", r"SOT-23-\1", s)
    s = re.sub(r"^SOT-?223(-3L?)?$", "SOT-223", s)
    s = re.sub(r"^SOT-?89(-3L?)?$", "SOT-89", s)
    if re.match(r"^(SO|SOP|SOIC)-?\d+-(208|300)MIL", s):
        s = re.sub(r"^(SO|SOP|SOIC)-?(\d+).*$", r"SOIC-\2W", s)
    s = re.sub(r"^(SO|SOP|SOIC)-?(\d+)(-(150|3\.9).*)?$", r"SOIC-\2", s)
    s = re.sub(r"^TO-?252.*$", "TO-252", s)
    return s


def chip_size(p: str | None) -> str | None:
    n = norm_pkg(p)
    return n if n in CHIP_SIZES else None


# ── classification ───────────────────────────────────────────────────────
KINDS = ["resistor", "capacitor", "electrolytic", "tantalum", "inductor", "ferrite", "led", "diode",
         "schottky", "zener", "tvs", "esd", "rectifier", "bridge", "mosfet", "bjt", "ldo", "dcdc",
         "crystal", "oscillator", "usb_connector", "header", "connector", "switch", "fuse",
         "mcu", "module", "memory", "opamp", "interface", "ic", "other"]


def classify(category: str = "", subcategory: str = "", desc: str = "", mfr: str = "") -> str:
    c = f"{category} | {subcategory}".lower()
    d = f"{desc} {mfr}".lower()
    if "resistor network" in c or "resistor array" in c:
        return "resistor"
    if "resistor" in c and "thermistor" not in c and "varistor" not in c:
        return "resistor"
    if "electrolytic" in c or "polymer aluminum" in c:
        return "electrolytic"
    if "tantalum" in c:
        return "tantalum"
    if "capacitor" in c or "mlcc" in c:
        return "capacitor"
    if "ferrite" in c:
        return "ferrite"
    if "inductor" in c or "chokes" in c:
        return "inductor"
    if "led" in c and ("indication" in c or "led" == subcategory.lower().strip() or "rgb" in c
                       or "infrared" in c or "light emitting" in c) and "driver" not in c:
        return "led"
    if "esd" in c or "tvs" in c:
        return "tvs" if "tvs" in d and "esd" not in d else "esd"
    if "schottky" in c:
        return "schottky"
    if "zener" in c:
        return "zener"
    if "bridge rectifier" in c:
        return "bridge"
    if "diode" in c or "rectifier" in c:
        return "diode"
    if "mosfet" in c:
        return "mosfet"
    if "bipolar" in c or "bjt" in c or "darlington" in c or "digital transistor" in c:
        return "bjt"
    if "ldo" in c or "linear" in c and "regulator" in c:
        return "ldo"
    if "dc-dc" in c or "switching regulator" in c or "buck" in c or "boost" in c:
        return "dcdc"
    if "crystal" in c and "oscillator" not in subcategory.lower():
        return "crystal"
    if "oscillator" in c or "resonator" in c:
        return "oscillator" if "crystal" not in subcategory.lower() else "crystal"
    if "usb" in c and "connector" in c:
        return "usb_connector"
    if "header" in c:
        return "header"
    if "connector" in c or "terminal" in c or "card sockets" in c or "battery holder" in c:
        return "connector"
    if "switch" in c and "analog" not in c and "load switch" not in c and "power distribution" not in c:
        return "switch"
    if "fuse" in c:
        return "fuse"
    if "module" in c or "wifi" in c or "bluetooth" in c and "module" in d:
        return "module"
    if "microcontroller" in c or "mcu" in c:
        return "mcu"
    if "memory" in c or "flash" in c or "eeprom" in c:
        return "memory"
    if "amplifier" in c or "comparator" in c:
        return "opamp"
    if "interface" in c or "usb converters" in c:
        return "interface"
    if c.strip(" |"):
        return "ic"
    return "other"


_COLORS = ["red", "green", "blue", "yellow", "white", "orange", "amber", "warm white", "pink", "purple",
           "rgb", "infrared", "uv", "emerald", "yellow-green"]


def led_color(text: str) -> str | None:
    """Normalised LED colour: red/green/blue/yellow/white/orange/yellow-green/warm-white/rgb/infrared/uv/..."""
    t = (text or "").lower().replace("_", " ")
    for pat, name in (("yellow-green", "yellow-green"), ("yellow green", "yellow-green"), ("warm white", "warm-white"),
                      ("warm-white", "warm-white"), ("rgb", "rgb"), ("infrared", "infrared"), ("ultraviolet", "uv"),
                      ("emerald", "green"), ("orange", "orange"), ("amber", "orange"), ("yellow", "yellow"),
                      ("white", "white"), ("green", "green"), ("blue", "blue"), ("red", "red"), ("pink", "pink"),
                      ("purple", "purple"), ("violet", "purple")):
        if pat in t:
            return name
    return None


_UNIT_RE = {"ohm": r"(?:Ω|Ω|ohms?|R)", "F": "F", "H": "H", "V": "V", "A": "A", "W": "W", "Hz": "Hz"}


def attr_value(attrs: dict, *names, unit=None):
    """First attribute among ``names`` (case-insensitive). With ``unit``, the
    first '<number><prefix><unit>' token in it, parsed to SI."""
    low = {str(k).lower(): v for k, v in (attrs or {}).items()}
    for n in names:
        v = low.get(n.lower())
        if v in (None, "", "-"):
            continue
        if unit is None:
            return v
        for m in re.finditer(r"([-+]?\d+(?:\.\d+)?)\s*([pnuµμmkKMG]?)\s*" + _UNIT_RE[unit], str(v)):
            return float(m.group(1)) * _PREFIX.get(m.group(2), 1.0)
    return None


def desc_currents(desc: str) -> list[float]:
    """Current ratings written in a JLC description ('2.2A 2.2A 22uH', '600mA'), in A.
    Skips 'mAh', '@(1A)' test conditions and 'A' inside words."""
    out = []
    for m in re.finditer(r"(?<![\w.@(])(\d+(?:\.\d+)?)\s*(m?)A(?![\w])", desc or ""):
        out.append(float(m.group(1)) * (1e-3 if m.group(2) else 1.0))
    return out


def typed_fields(kind: str, attrs: dict, desc: str = "", mfr: str = "") -> dict:
    """Normalized typed columns from JLC attributes / description."""
    t: dict = {}
    a = attrs or {}
    if kind == "resistor":
        t["ohm"] = attr_value(a, "Resistance", unit="ohm")
        tol = attr_value(a, "Tolerance")
        if tol:
            m = re.search(r"([\d.]+)\s*%", str(tol))
            if m:
                t["tol"] = float(m.group(1)) / 100
        t["W"] = attr_value(a, "Power(Watts)", "Power", unit="W")
    elif kind in ("capacitor", "electrolytic", "tantalum"):
        t["F"] = attr_value(a, "Capacitance", unit="F")
        t["V"] = attr_value(a, "Voltage Rating", "Rated Voltage", "Voltage Rated", unit="V")
        tc = attr_value(a, "Temperature Coefficient")
        if tc:
            t["diel"] = str(tc).upper().replace("NP0", "C0G")
        tol = attr_value(a, "Tolerance")
        if tol:
            m = re.search(r"([\d.]+)\s*%", str(tol))
            if m:
                t["tol"] = float(m.group(1)) / 100
    elif kind in ("inductor", "ferrite"):
        t["H"] = attr_value(a, "Inductance", unit="H")
        # usable current = min(rated/Irms, Isat): a power inductor is only good to the lower of the two
        cur = [x for x in (attr_value(a, "Rated Current", "Current Rating", unit="A"),
                           attr_value(a, "Current - Saturation(Isat)", "Saturation Current (Isat)",
                                      "Saturation Current", unit="A")) if x is not None]
        if not cur:
            cur = desc_currents(desc)
        t["A"] = min(cur) if cur else None
        imp = attr_value(a, "Impedance @ Frequency")
        if imp:
            m = re.match(r"\s*([\d.]+\s*[kKM]?)\s*Ω", str(imp))
            if m:
                t["ohm"] = parse_si(m.group(1).replace(" ", ""), "ohm")
    elif kind == "led":
        t["color"] = led_color(str(a.get("Illumination Color", "")) or desc)
    elif kind == "ldo":
        t["Vout"] = attr_value(a, "Output Voltage", unit="V")
        t["A"] = attr_value(a, "Output Current", unit="A")
        t["Vin_max"] = attr_value(a, "Voltage - Supply", "Maximum Input Voltage", "Input Voltage", unit="V")
    elif kind in ("crystal", "oscillator"):
        t["Hz"] = attr_value(a, "Frequency", unit="Hz")
        if t["Hz"] is None:
            m = re.search(r"([\d.]+)\s*(k|M)Hz", desc + " " + mfr, re.I)
            if m:
                t["Hz"] = float(m.group(1)) * (1e3 if m.group(2).lower() == "k" else 1e6)
        cl = attr_value(a, "Load Capacitance", unit="F")
        if cl is None:
            m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*pF\b", desc)
            cl = float(m.group(1)) * 1e-12 if m else None
        t["load_pF"] = round(cl * 1e12, 2) if cl is not None else None
    elif kind in ("mosfet",):
        typ = str(a.get("Type", "")) + " " + desc
        t["channel"] = "P" if re.search(r"p[- ]?(channel|ch)", typ, re.I) else ("N" if re.search(r"n[- ]?(channel|ch)", typ, re.I) else None)
        t["V"] = attr_value(a, "Drain to Source Voltage", "Drain Source Voltage (Vdss)", unit="V")
        t["A"] = attr_value(a, "Continuous Drain Current (Id)", "Current - Continuous Drain(Id)", unit="A")
    elif kind == "bjt":
        typ = str(a.get("Transistor Type", "")) + " " + desc
        t["polarity"] = "PNP" if "PNP" in typ.upper() else ("NPN" if "NPN" in typ.upper() else None)
    elif kind in ("diode", "schottky", "zener", "tvs", "esd", "rectifier"):
        t["V"] = attr_value(a, "Voltage - DC Reverse(Vr)", "Zener Voltage(Nom)", "Reverse Stand-Off Voltage (Vrwm)",
                            "Reverse Voltage (Vr)", unit="V")
        t["A"] = attr_value(a, "Current - Rectified", "Average Rectified Current (Io)", unit="A")
    return {k: v for k, v in t.items() if v is not None}


def gz_json_load(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def isfinite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
