"""Part record → KiCad stock symbol + footprint, with no supplier involved.

Used by every provider that does not ship its own drawings (the local catalogue, the generic KiCad
provider, third-party providers that only know MPNs):

  1. the record's own ``lib_id`` / ``footprint``                       (a catalogue row that names them)
  2. chip passives / LEDs / diodes by kind + package                  → Device:R + Resistor_SMD:R_0603_1608Metric
  3. pin headers / sockets by pin count and pitch                     → Connector_Generic:Conn_01x04 + PinHeader_1x04_P2.54mm_Vertical
  4. a KiCad symbol named after the MPN (AMS1117-3.3, USBLC6-2SC6 …)  with that symbol's default footprint
"""
from __future__ import annotations

import re

from .. import common as C

_KIND_BY_LIB = [(r"^Device:R(_|$)", "resistor"), (r"^Device:C_Polarized", "electrolytic"), (r"^Device:C(_|$)", "capacitor"),
                (r"^Device:L(_|$)", "inductor"), (r"^Device:LED", "led"), (r"^Device:D_Schottky", "schottky"),
                (r"^Device:D_Zener", "zener"), (r"^Device:D(_|$)", "diode"), (r"^Device:FerriteBead", "ferrite"),
                (r"^Device:Crystal", "crystal"), (r"^Connector", "header")]

UNIT_OF = {"resistor": "ohm", "capacitor": "F", "electrolytic": "F", "tantalum": "F", "inductor": "H", "ferrite": "ohm",
           "crystal": "Hz", "oscillator": "Hz"}


def kind_of(rec: dict) -> str:
    """Best guess of a record's kind from what it carries (explicit kind > symbol > value/package/description)."""
    k = str(rec.get("kind") or "").strip().lower()
    if k:
        return k
    lib_id = str(rec.get("lib_id") or "")
    for rx, kind in _KIND_BY_LIB:
        if re.search(rx, lib_id):
            return kind
    from .. import basic
    for text in (f"{rec.get('value') or ''} {rec.get('package') or ''}", str(rec.get("description") or "")):
        sp = basic.parse_spec(text)
        if sp.get("kind"):
            return sp["kind"]
    return ""


def value_text(spec: dict) -> str:
    """Schematic Value for a parsed spec: '10k', '100nF', 'LED red'."""
    k, v = spec.get("kind"), spec.get("value")
    if k == "led":
        return f"LED {spec.get('color')}".strip() if spec.get("color") else "LED"
    if v is not None and (spec.get("unit") or UNIT_OF.get(k)):
        return C.fmt_si(v, spec.get("unit") or UNIT_OF[k])
    return ""


def header(pins: int, rows: int = 1, pitch: float = 2.54, female: bool = False) -> dict | None:
    """KiCad's generic connector symbol + the matching vertical through-hole header footprint."""
    if not pins or pins < 1 or rows not in (1, 2):
        return None
    per_row = pins // rows if rows == 2 else pins
    if rows == 2 and pins % 2:
        return None
    if not (1 <= per_row <= 40):
        return None
    p = f"{pitch:.2f}"
    if p not in ("2.54", "2.00", "1.27", "1.00"):
        return None
    fam = "PinSocket" if female else "PinHeader"
    if rows == 1:
        sym = f"Connector_Generic:Conn_01x{per_row:02d}"
        fp = f"Connector_{fam}_{p}mm:{fam}_1x{per_row:02d}_P{p}mm_Vertical"
    else:
        sym = f"Connector_Generic:Conn_02x{per_row:02d}_Odd_Even"
        fp = f"Connector_{fam}_{p}mm:{fam}_2x{per_row:02d}_P{p}mm_Vertical"
    return {"lib_id": sym, "footprint": fp, "why": f"{rows}x{per_row} {p} mm {'socket' if female else 'pin header'}",
            "value": f"Conn_{rows:02d}x{per_row:02d}"}


def from_spec(spec: dict, default_package: str = "0603") -> dict | None:
    """A parsed spec (parts.basic.parse_spec) → {'lib_id','footprint','value','kind','package','why'} or None."""
    from .. import importer
    kind = spec.get("kind")
    if kind == "header":
        raw = str(spec.get("raw") or "").lower()
        h = header(int(spec.get("pins") or 0), int(spec.get("rows") or 1), float(spec.get("pitch") or 2.54),
                   female=bool(spec.get("female")) or "female" in raw or "socket" in raw)
        if h:
            return dict(h, kind="header", package="")
        return None
    if not kind:
        return None
    pkg = C.norm_pkg(spec.get("package")) or (default_package if kind in
                                               ("resistor", "capacitor", "inductor", "ferrite", "led", "fuse") else "")
    sc = importer.stock_candidate({"kind": kind, "package": pkg, "description": str(spec.get("raw") or "")})
    if not sc:
        return None
    return {"lib_id": sc["lib_id"], "footprint": sc["footprint"], "value": value_text(spec) or str(spec.get("raw") or ""),
            "kind": kind, "package": pkg, "why": sc["why"]}


_SMD_PKG = re.compile(r"^(SOT|SOD|SOIC|SOP|SSOP|TSSOP|MSOP|QFN|DFN|LQFP|TQFP|QFP|SMA|SMB|SMC|SC-|ESOP|LGA|BGA|WSON|SON|UFQFPN|VQFN|"
                      r"WQFN|TO-252|TO-263|DO-214)", re.I)
_THT_PKG = re.compile(r"^(TO-92|TO-220|TO-126|TO-247|TO-3P?|DIP|PDIP|SIP|DO-35|DO-41|DO-15|DO-201)", re.I)
_THT_FP = re.compile(r"(_THT:|^Package_DIP:|:DIP-|:TO-92|:TO-220|:TO-126|:TO-247|:D_DO-35|:D_DO-41)")


def _other_mounting(package, fp_id: str) -> bool:
    """The record names an SMD package and the symbol's default footprint is through-hole, or the reverse."""
    pkg = str(package or "").strip()
    if not pkg or not fp_id:
        return False
    tht_fp = bool(_THT_FP.search(fp_id))
    return bool((_SMD_PKG.match(pkg) and tht_fp) or (_THT_PKG.match(pkg) and not tht_fp))


def fit(rec: dict, project_dir=None) -> dict:
    """→ {'lib_id','footprint','source','why','warnings'}; lib_id is '' when nothing fits."""
    from .. import basic, importer
    warnings: list[str] = []
    lib_id, fp = str(rec.get("lib_id") or "").strip(), str(rec.get("footprint") or "").strip()
    if lib_id:
        why = "symbol named by the catalogue"
        if not fp:
            info = importer.stock_symbol(lib_id, project_dir)
            fp = (info or {}).get("footprint") or ""
            if not fp:
                warnings.append(f"{lib_id} has no default footprint — set one (catalogue 'footprint' column or set_field Footprint)")
        return {"lib_id": lib_id, "footprint": fp, "source": "catalogue", "why": why, "warnings": warnings}
    kind = kind_of(rec)
    sp = basic.parse_spec(f"{rec.get('value') or ''} {rec.get('package') or ''}".strip())
    sp["kind"] = kind or sp.get("kind")
    if rec.get("package"):
        sp["package"] = rec["package"]
    got = from_spec(sp)
    if got:
        return {"lib_id": got["lib_id"], "footprint": fp or got["footprint"], "source": "kicad-stock", "why": got["why"],
                "warnings": warnings}
    for name in (rec.get("mpn"), rec.get("value")):
        for lid in importer.find_stock_symbols(str(name or "")):
            info = importer.stock_symbol(lid, project_dir)
            if info:
                f2 = fp or info.get("footprint") or ""
                if not fp and _other_mounting(rec.get("package"), f2):
                    # an S8050 in SOT-23 is not KiCad's S8050 (TO-92, other pin order): no pin data here to prove a fit
                    warnings.append(f"{lid} is drawn for {f2}, not for package '{rec.get('package')}' — not used "
                                    f"(its pin order may differ); name a lib_id + footprint for this part")
                    continue
                if not f2:
                    warnings.append(f"{lid} has no default footprint — set one before the PCB")
                return {"lib_id": lid, "footprint": f2, "source": "kicad-stock",
                        "why": "KiCad symbol named after the part", "warnings": warnings}
    return {"lib_id": "", "footprint": fp, "source": None, "why": None, "warnings": warnings}
