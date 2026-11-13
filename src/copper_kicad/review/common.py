"""Shared helpers for the review package: findings, KiCad item-description
parsing, netlist loading (with a self-contained kicad-cli fallback), part
classification and small file helpers.

Nothing here imports pcbnew; everything runs in the server process.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .. import sexpr
from ..kicad import env
from ..mcp.registry import ToolError

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "exclusion": 3}


def finding(severity: str, ftype: str, message: str, fix: str | None = None, items: list | None = None,
            at: list | None = None, **extra) -> dict:
    """Canonical finding dict shared by verify / design_review / fab."""
    f = {"severity": severity, "type": ftype, "code": ftype, "message": message,
         "items": items or [], "at": at, "fix": fix}
    f.update({k: v for k, v in extra.items() if v is not None})
    return f


def sort_findings(fs: list[dict]) -> list[dict]:
    return sorted(fs, key=lambda f: (SEVERITY_ORDER.get(f.get("severity"), 9), f.get("type", ""), f.get("message", "")))


# ── KiCad violation item descriptions ─────────────────────────────────────
# ERC:  "Symbol U1 Pin 3 [GPIO, Bidirectional, Line]", "Symbol C2 [CP]",
#       "Global Label 'SDA'", "Label 'NET'", "Horizontal Wire, length 0.06 mm",
#       "Hierarchical Label 'X'", "Sheet 'power' [power.kicad_sch]", "Sheet pin 'X' of sheet ..."
# DRC:  "PTH pad 1 [+BATT] of J2", "Pad 2 [/Debugger/D-] of U3 on F.Cu", "Footprint J4",
#       "Track [GND] on F.Cu, length 0.44 mm", "Via [GND] on F.Cu - B.Cu", "Zone [GND] on F.Cu",
#       "Segment on Edge.Cuts"
# power symbols / hidden power pins: "Symbol #PWR02 Hidden pin 1 [+12V, Power input, Line]"
#   (the number can be empty for unnamed power-symbol pins: "Symbol #PWR0686 Hidden pin  [P3V3, …]")
_RE_PIN = re.compile(r"^Symbol\s+(?P<ref>\S+)\s+(?:Hidden\s+)?[Pp]in\s*(?P<pin>[^\s\[]*)(?:\s*\[(?P<info>[^\]]*)\])?")
_RE_SYM = re.compile(r"^Symbol\s+(?P<ref>\S+)(?:\s+\[(?P<value>[^\]]*)\])?")
_RE_LABEL = re.compile(r"^(?P<kind>(?:Global|Hierarchical|Local)?\s*Label|Power symbol|Directive Label|Net class flag)\s+'(?P<net>[^']*)'", re.I)
_RE_PAD = re.compile(r"^(?:(?P<kind>PTH|NPTH|SMD|Connector|Castellated|Aperture)\s+)?[Pp]ad\s+(?P<pad>\S*)\s*(?:\[(?P<net>[^\]]*)\]\s*)?of\s+(?P<ref>\S+)(?:\s+on\s+(?P<layer>[^,]+))?")
_RE_FP = re.compile(r"^Footprint\s+(?P<ref>\S+)")
# DRC also writes "Track (arc) [NET] on F.Cu, …" and "Zone 'name' [NET] on L10, priority 16"
_RE_TRACK = re.compile(r"^(?P<kind>Track|Via|Blind via|Buried via|Micro via|Blind/buried via|Microvia|Arc|Zone|Filled zone|Rule area)"
                       r"(?:\s*\(arc\))?(?:\s*'[^']*')?\s*(?:\[(?P<net>[^\]]*)\])?(?:\s+on\s+(?P<layer>[^,]+))?", re.I)


PIN_TYPES = {"input", "output", "bidirectional", "tri-state", "passive", "free", "unspecified", "power input",
             "power output", "open collector", "open emitter", "unconnected", "not connected", "no connect"}


def split_unit(ref: str) -> str:
    """'U1A' → 'U1' (multi-unit symbols are reported with the unit letter)."""
    m = re.match(r"^(.*\d)([A-Z])$", ref or "")
    return m.group(1) if m else ref


def parse_item(desc: str) -> dict:
    """Turn a KiCad violation item description into structured fields."""
    d = (desc or "").strip()
    out: dict = {"description": d}
    m = _RE_PIN.match(d)
    if m:
        info = [s.strip() for s in (m.group("info") or "").split(",")]
        ref = split_unit(m.group("ref"))
        pin = m.group("pin") or ("1" if ref.startswith("#") else "")
        out.update(kind="pin", ref=ref, pin=pin, pin_ref=f"{ref}.{pin}" if pin else ref)
        info = [s for s in info if s]
        # "[NAME, Type, Shape]" — NAME is omitted for unnamed/hidden pins (power symbols)
        if info and info[0].lower() not in PIN_TYPES:
            out["pin_name"] = info.pop(0)
        if info and info[0].lower() in PIN_TYPES:
            out["pin_type"] = info[0]
        return out
    m = _RE_SYM.match(d)
    if m:
        out.update(kind="symbol", ref=split_unit(m.group("ref")), value=m.group("value"))
        return out
    m = _RE_LABEL.match(d)
    if m:
        out.update(kind="label", label_kind=m.group("kind").strip().lower(), net=m.group("net"))
        return out
    m = _RE_PAD.match(d)
    if m:
        out.update(kind="pad", ref=m.group("ref"), pad=m.group("pad"), pin_ref=f"{m.group('ref')}.{m.group('pad')}")
        if m.group("net"):
            out["net"] = m.group("net")
        if m.group("layer"):
            out["layer"] = m.group("layer").strip()
        return out
    m = _RE_FP.match(d)
    if m:
        out.update(kind="footprint", ref=m.group("ref"))
        return out
    m = _RE_TRACK.match(d)
    if m:
        out.update(kind=m.group("kind").lower().replace(" ", "_"))
        if m.group("net"):
            out["net"] = m.group("net")
        if m.group("layer"):
            out["layer"] = m.group("layer").strip()
        return out
    if "wire" in d.lower():
        out["kind"] = "wire"
    elif d.lower().startswith("sheet"):
        out["kind"] = "sheet"
    elif d.lower().startswith("segment") or d.lower().startswith("line") or "edge.cuts" in d.lower():
        out["kind"] = "graphic"
    else:
        out["kind"] = "other"
    return out


# ── netlist ───────────────────────────────────────────────────────────────
def _fields_of(comp) -> dict:
    fields = {}
    fl = sexpr.find(comp, "fields")
    if fl is not None:
        for f in sexpr.find_all(fl, "field"):
            name = sexpr.value(f, "name")
            val = f[2] if len(f) > 2 and not isinstance(f[2], list) else ""
            if name:
                fields[str(name)] = str(val)
    return fields


def parse_netlist_text(text: str) -> dict:
    """kicadsexpr netlist → {'components':{ref:{...}}, 'nets':{name:[{ref,pin,pin_name,pin_type}]}}"""
    tree = sexpr.loads(text)
    comps: dict = {}
    cs = sexpr.find(tree, "components")
    for c in (sexpr.find_all(cs, "comp") if cs is not None else []):
        ref = str(sexpr.value(c, "ref", ""))
        fields = _fields_of(c)
        props = {}
        for p in sexpr.find_all(c, "property"):
            props[str(sexpr.value(p, "name", ""))] = str(sexpr.value(p, "value", "") or "")
        ls = sexpr.find(c, "libsource")
        lib = str(sexpr.value(ls, "lib", "") or "") if ls is not None else ""
        part = str(sexpr.value(ls, "part", "") or "") if ls is not None else ""
        desc = str(sexpr.value(ls, "description", "") or "") if ls is not None else ""
        sp = sexpr.find(c, "sheetpath")
        comps[ref] = {
            "value": str(sexpr.value(c, "value", "") or ""),
            "footprint": str(sexpr.value(c, "footprint", "") or ""),
            "lib_id": f"{lib}:{part}" if lib else part,
            "fields": fields,
            "description": fields.get("Description") or desc,
            "sheet": str(sexpr.value(sp, "names", "/")) if sp is not None else "/",
            "dnp": "dnp" in props,
            "exclude_from_bom": "exclude_from_bom" in props,
            "exclude_from_board": "exclude_from_board" in props,
        }
    nets: dict = {}
    ns = sexpr.find(tree, "nets")
    for n in (sexpr.find_all(ns, "net") if ns is not None else []):
        name = str(sexpr.value(n, "name", ""))
        nodes = []
        for nd in sexpr.find_all(n, "node"):
            pin = str(sexpr.value(nd, "pin", ""))
            pf = str(sexpr.value(nd, "pinfunction", "") or "")
            if pf.endswith(f"_{pin}"):      # KiCad 10 appends the pin number when a symbol repeats pin names
                pf = pf[: -len(pin) - 1]
            nodes.append({"ref": str(sexpr.value(nd, "ref", "")), "pin": pin,
                          "pin_name": pf,
                          "pin_type": str(sexpr.value(nd, "pintype", "") or "")})
        nets[name] = nodes
    return {"components": comps, "nets": nets}


def cli(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """Run kicad-cli: a thin alias of env.run(), the single launcher (file-format
    preflight, version-gated flags, detached stdin, KICAD_CONFIG_HOME untouched)."""
    return env.run(args, timeout=timeout)


def work_dir(pr) -> Path:
    """Where Copper keeps per-project state (.copper/). Falls back to a cache
    folder under COPPER_HOME when the project folder is read-only (e.g. the
    KiCad demos under Program Files) so read-only checks still work."""
    try:
        d = pr.copper_dir
        probe = d / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return d
    except OSError:
        import hashlib
        from .. import project as _P
        h = hashlib.sha1(str(pr.dir).lower().encode("utf-8")).hexdigest()[:12]
        d = _P.STATE_DIR / "cache" / f"{pr.name}-{h}"
        d.mkdir(parents=True, exist_ok=True)
        return d


def out_dir(pr) -> Path:
    """<project>/copper-out, or a COPPER_HOME cache folder for read-only projects."""
    try:
        d = pr.out_dir
        probe = d / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return d
    except OSError:
        d = work_dir(pr) / "copper-out"
        d.mkdir(parents=True, exist_ok=True)
        return d


def netlist_via_cli(pr) -> dict:
    if not pr.sch.exists():
        raise ToolError(f"no schematic at {pr.sch}", code="NO_SCHEMATIC",
                        fix="project_create(...) or open a project that has a .kicad_sch")
    fd, tmp = tempfile.mkstemp(suffix=".net", prefix="copper-")
    os.close(fd)
    try:
        r = cli(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", tmp, str(pr.sch)], timeout=240)
        text = Path(tmp).read_text(encoding="utf-8", errors="replace") if os.path.exists(tmp) else ""
        if not text.strip():
            raise ToolError(f"kicad-cli could not export the netlist: {(r.stderr or r.stdout).strip()[-600:]}",
                            code="NETLIST_FAILED", fix="verify(what=['erc']) to see what KiCad dislikes in the schematic")
        return parse_netlist_text(text)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


_NL_CACHE: dict = {}


def load_netlist(pr) -> dict:
    """Netlist for the project, cached per schematic revision.

    Parses KiCad's own kicadsexpr netlist. The shape is the contract shape
    (sch.netlist.export_netlist) plus 'dnp', 'exclude_from_bom', 'exclude_from_board' and
    'description' per component."""
    key = (str(pr.sch), _sheets_signature(pr))
    if key in _NL_CACHE:
        return _NL_CACHE[key]
    # One kicad-cli call, parsed here: KiCad's own netlist carries the DNP /
    # exclude-from-BOM flags and pin types the review needs.
    nl = netlist_via_cli(pr)
    _NL_CACHE.clear()
    _NL_CACHE[key] = nl
    return nl


def _sheets_signature(pr) -> str:
    parts = []
    try:
        for f in pr.sheets():
            st = f.stat()
            parts.append(f"{f.name}:{st.st_mtime_ns}:{st.st_size}")
    except OSError:
        pass
    return "|".join(parts)


# ── part / net classification ─────────────────────────────────────────────
LCSC_FIELD_NAMES = ("lcsc", "lcsc part", "lcsc part #", "lcsc#", "lcsc_part", "lcsc part number", "lcscpart",
                    "lcsc_pn", "lcsc pn", "lcsc_part_number", "lcsc part no", "lcsc_part_no", "lcsc id", "lcsc_id",
                    "jlcpcb part #", "jlcpcb part", "jlc part", "jlc", "jlcpcb", "jlc_part", "jlc part #", "jlcpcb_part",
                    "jlc_pn", "jlcpcb pn")


def lcsc_of(fields: dict) -> str:
    for k, v in (fields or {}).items():
        if k.strip().lower() in LCSC_FIELD_NAMES and v and str(v).strip():
            m = re.search(r"C\d{2,}", str(v).strip().upper())
            if m:
                return m.group(0)
    return ""


def field_ci(fields: dict, *names: str) -> str:
    low = {k.strip().lower(): v for k, v in (fields or {}).items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, ""):
            return str(v)
    return ""


def ref_prefix(ref: str) -> str:
    m = re.match(r"^#?([A-Za-z_]+)", ref or "")
    return m.group(1).upper() if m else ""


def base_net(name: str) -> str:
    """'/sub/SDA' → 'SDA'."""
    return (name or "").rstrip("/").split("/")[-1]


_GND_RE = re.compile(r"^(?:A|D|P|S|C)?GND(?:[_-]?\w*)?$|^(?:GND|VSS)\w*$|^0V$|^EARTH$|^CHASSIS$", re.I)
# rails: +5V, +3V3, +5F, -12V, 3V3, 3.3V, 1V8_IO, 5V_USB, VCC, VDD_CORE, VBUS, PWR, PWR_5V … — but NOT 14MHZOUT / 2N2222_B / PWR_LED
_PWR_RE = re.compile(r"^[+-]\d[\w.]*$|^\d+(?:\.\d+)?V\d*(?:_\w*)?$|^(?:VCC|VDD|VBUS|VBAT|VIN|VSYS|VREF|AVDD|DVDD|IOVDD|VDDIO|VDDA|"
                     r"VCCIO|V3V3|V5V|VPP|VM|VMOT|VLED|V\+|VEE|VSS)\w*$|^PWR(?:[_-]?(?:\d[\w.]*|IN|OUT|MAIN|SYS|BUS))?$", re.I)


_NOT_RAIL_RE = re.compile(r"[_\-](?:ON|OFF|EN|ENABLE|OK|PG|PGOOD|GOOD|DET|DETECT|SENSE|SNS|FB|CTRL|SW|ADC|MON|FLT|FAULT|ALERT|INT|N|"
                          r"LED|IND|STAT|STATUS|CHG|CHRG)$", re.I)
# pin names of an IC's own ground/reference pin
_GND_PIN_RE = re.compile(r"^(?:A|D|P|S|C)?GND\d*$|^VSS\w*$|^0V$|^GND_\w+$", re.I)


def _pin_names(n: dict) -> list[str]:
    name = re.sub(r"~\{([^}]*)\}", r"\1", n.get("pin_name") or "").replace("~", "")
    return [x.strip() for x in re.split(r"[/,]", name) if x.strip()]


def is_ground_pin(n: dict) -> bool:
    return any(_GND_PIN_RE.match(x) for x in _pin_names(n))


# Pin types that only a part with electronics inside has. A part with none of these (every pin passive,
# power_in or no-connect) and no ground pin of its own is an element or a load, whatever its reference says.
_ACTIVE_PIN_TYPES = frozenset(("input", "output", "bidirectional", "tri_state", "3state", "open_collector", "open_emitter",
                               "power_out"))
_INERT_PIN_TYPES = frozenset(("passive", "power_in", "no_connect", "unconnected", "not_connected"))
# terminals of a heater, filament, coil or motor winding: H, H1, VH+, VH-, HEATER, HTR2, FIL1, COIL+, M+, MOTOR-
_LOAD_PIN_RE = re.compile(r"^(?:V?H\d*[+-]?|V?H[+-]\d*|V?HEAT(?:ER)?[\w+-]*|V?HTR[\w+-]*|FIL(?:AMENT)?[\w+-]*|COIL[\w+-]*|"
                          r"M(?:OT(?:OR)?)?\d*[+-])$", re.I)
# canonical IC supply pin names (the decoupling check reads the same list)
SUPPLY_PIN_RE = re.compile(r"^(?:VCC|VDD|AVDD|DVDD|VDDA|VDDIO|IOVDD|VCCIO|VIO|VCCA|VCC\d|VDD\d|VDD_\w+|VCC_\w+|V\+|VS|VBAT|3V3|VDDD|"
                           r"VDDPLL|VDDCORE|VCCINT|VCCAUX)$")
_LOAD_TEXT_RE = re.compile(r"\b(?:heater|heating element|motor|fan|blower|pump|buzzer|sounder|speaker|loudspeaker|lamp|bulb|"
                           r"filament|relay|solenoid|peltier|vibrator|gas sensor|electrochemical|thermistor|"
                           r"photoresistor|strain gauge|load cell|electret)s?\b", re.I)
_NOT_LOAD_TEXT_RE = re.compile(r"\b(?:driver|controller|control|amplifier|interface|digital|i2c|spi|uart|mcu|regulator|monitor|"
                               r"module|ic)\b", re.I)


def is_load_pin(n: dict) -> bool:
    """A heater / filament / coil / winding terminal by name (H, VH+, HEATER, COIL-, M+). KiCad draws an MQ
    gas sensor's heater pins as power inputs; they take supply current but are a resistive load, not an IC rail."""
    return any(_LOAD_PIN_RE.match(x) for x in _pin_names(n))


def is_supply_pin(n: dict) -> bool:
    """A pin that draws an IC's supply: typed power input (not a ground, not a load terminal) or carrying a
    canonical supply name (VCC, VDD, V+ …) whatever its type."""
    if is_load_pin(n) or is_ground_pin(n):
        return False
    return n.get("pin_type") == "power_in" or any(SUPPLY_PIN_RE.match(x.upper()) for x in _pin_names(n))


def is_load_part(comp: dict | None, pins: list | None) -> bool:
    """True for a part that has no electronics to decouple even though it is drawn like an IC (reference U/M/A,
    'power input' pins): a gas-sensor heater, motor, fan, buzzer, lamp, relay coil, bare sensing element.

    Decided from the pins first, the symbol text last:
      - any input/output/bidirectional/open-collector/power-output pin → it has electronics: not a load
      - its own GND/VSS pin → it has an internal reference (a module, a brushless fan): not a load
      - a heater/coil/winding terminal (is_load_pin), or the symbol says it is a load → load
      - a canonical supply pin (VCC, VDD, V+) → an IC drawn with passive pins: not a load
      - otherwise a load only when every pin is passive / power input / no-connect (nothing is 'unspecified')."""
    pins = pins or []
    if not pins:
        return False
    types = {str(p.get("pin_type") or "").lower().split("+")[0] for p in pins}
    if types & _ACTIVE_PIN_TYPES or any(is_ground_pin(p) for p in pins):
        return False
    if any(is_load_pin(p) for p in pins):
        return True
    c = comp or {}
    text = f"{(c.get('lib_id') or '').replace('_', ' ').replace(':', ' ')} {c.get('description') or ''}"
    if _LOAD_TEXT_RE.search(text) and not _NOT_LOAD_TEXT_RE.search(text):
        return True
    if any(SUPPLY_PIN_RE.match(x.upper()) for p in pins for x in _pin_names(p)):
        return False
    return types <= _INERT_PIN_TYPES


def is_ground_ref_net(nodes: list | None) -> bool:
    """A net that an IC uses as its *ground* (a GND/VSS pin sits on it) and that no
    supply pin uses — e.g. a battery-protection IC's GND on cell-negative (BATN),
    or an isolated/analog ground with its own name. It is a reference, not a rail."""
    if not nodes:
        return False
    gnd = [n for n in nodes if is_ground_pin(n) and n.get("pin_type") in ("power_in", "power_out", "passive", "")]
    if not gnd:
        return False
    return not any(n.get("pin_type") in ("power_in", "power_out") and _pin_names(n) and not is_ground_pin(n)
                   and not is_load_pin(n) and not str(n.get("ref", "")).startswith("#") for n in nodes)


def is_ground_net(name: str, nodes: list | None = None) -> bool:
    """By name (GND, AGND, /sub/GNDA, VSS, 0V …); with `nodes`, also a net used as an IC's
    ground reference under another name (see is_ground_ref_net)."""
    b = base_net(name)
    if _GND_RE.match(b):
        return True
    if nodes is None or is_auto_net(name) or (_PWR_RE.match(b) and not _NOT_RAIL_RE.search(b)):
        return False
    return is_ground_ref_net(nodes)


def is_auto_net(name: str) -> bool:
    return name.startswith("Net-(") or name.startswith("unconnected-(") or "Net-(" in name


def is_power_net(name: str, nodes: list | None = None) -> bool:
    if is_auto_net(name):
        # unnamed net that still feeds power pins (e.g. regulator output tied straight to IC VDD)
        return bool(nodes) and any(n.get("pin_type") == "power_out" for n in nodes)
    if is_ground_net(name):
        return False
    b = base_net(name)
    # VPP_ON, VBUS_DET, VCC_EN, VIN_SENSE … are control/sense signals named after a rail, not rails
    if _PWR_RE.match(b) and not re.match(r"^\d+$", b) and not _NOT_RAIL_RE.search(b):
        return True
    if nodes and is_ground_ref_net(nodes):
        return False            # BATN on a DW01A GND pin: a ground reference, not a supply
    # a heater/coil terminal typed 'power input' does not make its (switched) node a rail
    return bool(nodes) and any(n.get("pin_type") in ("power_in", "power_out") and not is_load_pin(n) for n in nodes)


def comp_kind(ref: str, comp: dict) -> str:
    """Rough role classification used by the heuristics."""
    lib = (comp.get("lib_id") or "").lower()
    part = lib.split(":")[-1]
    val = (comp.get("value") or "").lower()
    desc = (comp.get("description") or "").lower()
    pre = ref_prefix(ref)
    text = f"{lib} {val} {desc}"
    if pre in ("C", "CV", "CT", "CP", "CE", "CAP") or part in ("c", "c_small", "cp", "cp_small", "c_polarized", "c_polarized_small",
                                                         "csmall", "cpsmall", "c_variable", "c_trim") or part.startswith("c_"):
        return "capacitor"
    if pre in ("R", "RN") or part in ("r", "r_small", "r_us", "rsmall"):
        return "resistor"
    if pre in ("L", "FB") or part.startswith("l") and part in ("l", "l_small", "ferritebead", "ferritebead_small"):
        return "inductor"
    if pre in ("Y", "X") or "crystal" in text or "resonator" in text:
        if "oscillator" in text and "crystal" not in part:
            return "oscillator"
        return "crystal"
    if re.search(r"(?:^|[^a-z])led(?:[^a-z]|$)", part) or pre == "LED" or (pre == "D" and re.search(r"\bled\b", text)):
        return "led"
    if pre in ("D", "TVS", "ESD") or "diode" in text:
        return "diode"
    if pre in ("J", "P", "CN", "USB", "CON") or "connector" in lib or lib.startswith("connector"):
        return "connector"
    if pre in ("SW", "S", "BTN", "K"):
        return "switch"
    if pre in ("Q", "T"):
        return "transistor"
    if pre in ("U", "IC", "M", "MOD", "A"):
        return "ic"
    if pre in ("TP",):
        return "testpoint"
    if pre in ("H", "MH", "FID", "LOGO", "G", "REF"):
        return "mechanical"
    if pre in ("F",):
        return "fuse"
    return "other"


_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1, "r": 1}


def parse_value(v: str) -> float | None:
    """'4k7' → 4700, '5.1k' → 5100, '100nF' → 1e-7, '10uF 16V' → 1e-5, '0R' → 0."""
    if not v:
        return None
    s = v.strip().split()[0].replace(",", ".").replace("Ω", "").replace("ohm", "").replace("Ohm", "")
    m = re.match(r"^(\d+)([pnuµμmkKMGRr])(\d+)", s)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _SI[m.group(2)]
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([pnuµμmkKMGRr])?", s)
    if not m:
        return None
    return float(m.group(1)) * (_SI.get(m.group(2), 1) if m.group(2) else 1)


def natural_key(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref or "")]


# ── state file (.copper/verify.json) ──────────────────────────────────────
def state_path(pr) -> Path:
    return work_dir(pr) / "verify.json"


def load_state(pr) -> dict:
    try:
        return json.loads(state_path(pr).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(pr, key: str, value: dict):
    st = load_state(pr)
    st[key] = value
    try:
        state_path(pr).write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
