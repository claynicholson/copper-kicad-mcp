"""Builds the bundled offline parts table ``data/basic_parts.json.gz``.

    python -m copper_kicad.parts.seed_build [--csv path] [--no-curated]

Inputs:
  1. CDFER/jlcpcb-parts-database ``jlcpcb-components-basic-preferred.csv``
     (MIT, daily): every JLC Basic + Preferred part (~1.6k rows).
  2. A curated query list of the *Extended* parts that most hobby/pro boards
     still need (USB-C receptacles, headers, JST, electrolytics, popular
     LDOs/ESD/USB-UART/MCUs/modules/flash, crystals, switches...). Each query
     is run against the JLC parts API (1 req/s) and the best-stocked matching
     rows are kept and flagged ``cur``.

The output is small (<1.5 MB gz) and only used for instant offline ranking;
live JLC stock always overrides it at search time.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import time
from pathlib import Path

from . import common as C

CSV_URL = "https://cdfer.github.io/jlcpcb-parts-database/jlcpcb-components-basic-preferred.csv"
OUT = Path(__file__).resolve().parent.parent / "data" / "basic_parts.json.gz"

# (keyword, how many to keep, required subcategory substring (lowercase) or None, require mfr contains keyword?)
CURATED: list[tuple[str, int, str | None, bool]] = [
    # USB / connectors
    ("TYPE-C-31-M-12", 1, "usb", True), ("TYPE-C 16P", 4, "usb", False), ("TYPE-C 6P", 2, "usb", False),
    ("TYPE-C-31-M-17", 1, "usb", True), ("USB4085", 1, "usb", True), ("Micro USB", 2, "usb", False),
    ("Header 1x2P 2.54mm", 1, "header", False), ("Header 1x3P 2.54mm", 1, "header", False),
    ("Header 1x4P 2.54mm", 1, "header", False), ("Header 1x5P 2.54mm", 1, "header", False),
    ("Header 1x6P 2.54mm", 1, "header", False), ("Header 1x8P 2.54mm", 1, "header", False),
    ("Header 1x10P 2.54mm", 1, "header", False), ("Header 2x3P 2.54mm", 1, "header", False),
    ("Header 2x5P 2.54mm", 1, "header", False), ("Header 1x40P 2.54mm", 1, "header", False),
    ("Female Header 1x4P 2.54mm", 1, "header", False), ("Header 1x4P 1.27mm", 1, "header", False),
    ("PH2.0 2P", 2, "wire to board", False), ("PH2.0 4P", 1, "wire to board", False),
    ("XH2.54 2P", 2, "wire to board", False), ("SH1.0 4P", 2, "wire to board", False),
    ("KF301-2P", 1, "terminal", False), ("Micro SD", 2, "card", False), ("CR2032", 2, "battery", False),
    # electrolytics / bulk caps
    ("10uF 25V", 2, "electrolytic", False), ("22uF 35V", 1, "electrolytic", False), ("47uF 16V", 2, "electrolytic", False),
    ("100uF 16V", 2, "electrolytic", False), ("100uF 25V", 2, "electrolytic", False), ("220uF 16V", 2, "electrolytic", False),
    ("220uF 25V", 1, "electrolytic", False), ("470uF 16V", 1, "electrolytic", False), ("470uF 25V", 1, "electrolytic", False),
    ("1000uF 16V", 1, "electrolytic", False), ("10uF 50V", 1, "electrolytic", False),
    # MLCC that commonly aren't Basic
    ("22uF 0805", 2, "multilayer", False), ("10uF 0603", 2, "multilayer", False), ("47uF 1206", 1, "multilayer", False),
    # regulators
    ("ME6211C33M5G-N", 1, None, True), ("AP2112K-3.3TRG1", 1, None, True), ("RT9013-33GB", 1, None, True),
    ("HT7333-A", 1, None, True), ("AMS1117-5.0", 1, None, True), ("AMS1117-1.8", 1, None, True),
    ("AMS1117-ADJ", 1, None, True), ("AMS1117-3.3", 2, None, True), ("XC6220B331MR-G", 1, None, True),
    ("TLV75533PDBVR", 1, None, True), ("MCP1700T-3302E", 1, None, True), ("LM1117MPX-3.3", 1, None, True),
    ("AP7361C-33E", 1, None, True), ("LP5907MFX-3.3", 1, None, True), ("SPX3819M5-L-3-3", 1, None, True),
    ("MP1584EN", 1, None, True), ("TPS563200DDCR", 1, None, True), ("MT3608", 1, None, True),
    ("SY8089AAAC", 1, None, True), ("TPS54331DR", 1, None, True), ("LM2596S-5.0", 1, None, True),
    ("XL1509-5.0", 1, None, True), ("TP4056", 1, None, True), ("DW01A", 1, None, True), ("FS8205A", 1, None, True),
    ("MCP73831T-2ACI", 1, None, True), ("TPS2113A", 1, None, True),
    # protection
    ("USBLC6-2SC6", 1, None, True), ("SRV05-4", 1, None, True), ("PESD5V0S1BL", 1, None, True),
    ("TPD2E2U06", 1, None, True), ("ESD5Z5.0", 1, None, True), ("SMBJ5.0A", 1, None, True), ("SMAJ15A", 1, None, True),
    ("PRTR5V0U2X", 1, None, True), ("SMD1206", 2, "fuse", False), ("0805 PTC", 2, "fuse", False),
    # diodes / discretes
    ("SS14", 1, None, True), ("SS34", 1, None, True), ("B5819W", 1, None, True), ("1N5819", 1, None, True),
    ("BAT54C", 1, None, True), ("BAT54S", 1, None, True), ("BAT760", 1, None, True), ("M7", 1, "diode", True),
    ("1N4007", 1, None, True), ("AO3401A", 1, None, True), ("AO3400A", 1, None, True), ("SI2301", 1, None, True),
    ("SI2302", 1, None, True), ("BSS138", 1, None, True), ("BSS84", 1, None, True), ("DMG2305UX", 1, None, True),
    ("AO4407A", 1, None, True), ("IRLML6402", 1, None, True), ("2N7002", 1, None, True), ("MMBT3906", 1, None, True),
    ("BC847", 1, None, True), ("BC857", 1, None, True),
    # LEDs
    ("WS2812B", 2, None, True), ("SK6812MINI", 1, None, True), ("XL-1608", 24, "led indication", False),
    ("XL-2012", 14, "led indication", False), ("XL-1005", 7, "led indication", False), ("KT-0603", 7, "led indication", False),
    ("KT-0805", 6, "led indication", False), ("XL-1608RGBC", 1, None, True),
    # USB-UART / interface
    ("CH340C", 1, None, True), ("CH340N", 1, None, True), ("CH340G", 1, None, True), ("CH340E", 1, None, True),
    ("CP2102N-A02-GQFN24", 1, None, True), ("CP2102-GMR", 1, None, True), ("FT232RL", 1, None, True),
    ("SP3485EN", 1, None, True), ("MAX3485", 1, None, True), ("TJA1050", 1, None, True), ("SN65HVD230", 1, None, True),
    ("MCP2515", 1, None, True), ("TXS0108E", 1, None, True), ("TXB0104", 1, None, True), ("74HC595", 1, None, True),
    ("PCA9685", 1, None, True), ("CH9102F", 1, None, True), ("TS5A3166", 1, None, True),
    # MCUs / modules / memory
    ("RP2040", 1, None, True), ("RP2350A", 1, None, True), ("ESP32-C3-MINI-1-N4", 1, None, True),
    ("ESP32-C3FH4", 1, None, True), ("ESP32-C3-WROOM-02-N4", 1, None, True), ("ESP32-S3-WROOM-1-N8R2", 1, None, True),
    ("ESP32-S3-WROOM-1-N16R8", 1, None, True), ("ESP32-S3-MINI-1-N8", 1, None, True), ("ESP32-WROOM-32E-N4", 1, None, True),
    ("ESP32-WROOM-32-N4", 1, None, True), ("ESP32-C6-MINI-1", 1, None, True), ("ESP32-S2-MINI-1", 1, None, True),
    ("STM32F103C8T6", 1, None, True), ("STM32G030F6P6", 1, None, True), ("STM32G031K8", 1, None, True),
    ("STM32F411CEU6", 1, None, True), ("STM32F401CCU6", 1, None, True), ("STM32G431CBU6", 1, None, True),
    ("STM32F030F4P6", 1, None, True), ("STM32H750VBT6", 1, None, True), ("STM32L432KCU6", 1, None, True),
    ("ATMEGA328P-AU", 1, None, True), ("ATTINY85-20SU", 1, None, True), ("ATMEGA32U4-AU", 1, None, True),
    ("CH32V003F4P6", 1, None, True), ("CH32V203", 1, None, True), ("NRF52840", 1, None, True),
    ("W25Q128JVSIQ", 1, None, True), ("W25Q16JVSSIQ", 1, None, True), ("W25Q32JVSSIQ", 1, None, True),
    ("W25Q64JVSSIQ", 1, None, True), ("AT24C02", 1, None, True), ("AT24C256", 1, None, True),
    # sensors / misc ICs
    ("BME280", 1, None, True), ("SHT30-DIS", 1, None, True), ("SHTC3", 1, None, True), ("MPU-6050", 1, None, True),
    ("LIS3DH", 1, None, True), ("INA219", 1, None, True), ("ADS1115", 1, None, True), ("DS3231", 1, None, True),
    ("NE555", 1, None, True), ("LM358", 1, None, True), ("MCP6002", 1, None, True), ("LMV321", 1, None, True),
    ("TL431", 1, None, True), ("PCF8563", 1, None, True), ("DRV8833", 1, None, True), ("A4988", 1, None, True),
    ("ULN2003", 1, None, True),
    # crystals / oscillators
    ("32.768kHz", 2, "crystal", False), ("8MHz 3225", 1, "crystal", False), ("12MHz 3225", 1, "crystal", False),
    ("16MHz 3225", 1, "crystal", False), ("25MHz 3225", 1, "crystal", False), ("40MHz 3225", 1, "crystal", False),
    ("24MHz 3225", 1, "crystal", False), ("26MHz 3225", 1, "crystal", False),
    # switches / misc
    ("TS-1187A", 1, "switch", False), ("tactile switch", 4, "switch", False), ("SS-12D00G3", 1, "switch", False),
    ("MSK12C02", 1, "switch", False), ("Buzzer", 1, None, False), ("1uH", 3, "inductor", False), ("2.2uH", 4, "inductor", False),
    ("4.7uH", 4, "inductor", False), ("10uH", 4, "inductor", False), ("22uH", 3, "inductor", False), ("47uH", 2, "inductor", False),
]


def _row_from_csv(r: dict) -> dict | None:
    if r.get("present") == "0":
        return None
    try:
        attrs = json.loads(r.get("attributes") or "{}")
    except ValueError:
        attrs = {}
    rec = {
        "lcsc": C.lcsc_norm(r["lcsc"]), "mfr": r.get("mfr") or "", "manufacturer": r.get("manufacturer") or "",
        "package": r.get("package") or "", "category": r.get("category") or "", "subcategory": r.get("subcategory") or "",
        "description": r.get("description") or "", "basic": r.get("library_type") == "base",
        "preferred": r.get("preferred") == "1", "stock": int(r["stock"]) if (r.get("stock") or "").isdigit() else None,
        "price": C.price_ladder(r.get("price") or ""), "datasheet": r.get("datasheet") or "", "attributes": attrs,
    }
    if not rec["category"] and not rec["description"]:
        return None
    return rec


def _compact(rec: dict, curated=False) -> dict:
    d = {"c": rec["lcsc"], "m": rec["mfr"], "g": rec["manufacturer"], "p": rec["package"],
         "cat": rec["category"], "sub": rec["subcategory"], "d": rec["description"][:300],
         "b": 1 if rec["basic"] else 0, "pr": 1 if rec["preferred"] else 0, "s": rec["stock"],
         "$": [[p["qty"], p["usd"]] for p in rec["price"]][:6], "ds": rec["datasheet"],
         "a": rec["attributes"]}
    if curated:
        d["cur"] = 1
    return d


def build(csv_path: str | None = None, curated: bool = True, verbose=True) -> dict:
    if csv_path:
        text = Path(csv_path).read_text(encoding="utf-8")
    else:
        text = C.http(CSV_URL, timeout=90, retries=2).decode("utf-8")
    rows = {}
    for r in csv.DictReader(io.StringIO(text)):
        rec = _row_from_csv(r)
        if rec and rec["lcsc"]:
            rows[rec["lcsc"]] = _compact(rec)
    n_csv = len(rows)
    if verbose:
        print(f"CSV: {n_csv} basic/preferred parts", file=sys.stderr)
    added = 0
    if curated:
        from .search import jlc_query
        for kw, keep, sub, mfr_must in CURATED:
            try:
                res = jlc_query(kw, page_size=50, in_stock=True, ttl=6 * 3600)
            except C.NetError as e:
                print(f"  ! {kw}: {e}", file=sys.stderr)
                continue
            cand = []
            for r in res:
                if sub and "led" in sub and (r.get("stock") or 0) < 10000:
                    continue
                if sub and sub not in (r["subcategory"] + " " + r["category"]).lower():
                    continue
                if mfr_must and _alnum(kw) not in _alnum(r["mfr"]):
                    continue
                if (r.get("stock") or 0) < 100:
                    continue
                cand.append(r)
            exact = [r for r in cand if _alnum(r["mfr"]) == _alnum(kw)]
            cand.sort(key=lambda r: (0 if r in exact else 1, C.CLASS_RANK[r["class"]], -(r["stock"] or 0)))
            for r in cand[:keep]:
                if r["lcsc"] not in rows:
                    rows[r["lcsc"]] = _compact(r, curated=True)
                    added += 1
                else:
                    rows[r["lcsc"]]["cur"] = 1
            if verbose:
                print(f"  {kw!r}: {[ (r['lcsc'], r['mfr'], r['stock']) for r in cand[:keep]]}", file=sys.stderr)
    data = {"meta": {"built": time.strftime("%Y-%m-%d"), "source": "CDFER jlcpcb-parts-database basic-preferred CSV + "
                     "curated JLC API queries", "csv_parts": n_csv, "curated_added": added, "count": len(rows),
                     "note": "stock/price are a snapshot — live JLC data overrides at search time"},
            "parts": sorted(rows.values(), key=lambda d: int(d["c"][1:]))}
    return data


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def write(data: dict, out: Path = OUT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(out, "wb", compresslevel=9) as f:
        f.write(raw)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--no-curated", action="store_true")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    d = build(a.csv, curated=not a.no_curated)
    p = write(d, Path(a.out))
    print(f"wrote {p} ({p.stat().st_size/1024:.0f} KB, {d['meta']['count']} parts)")
