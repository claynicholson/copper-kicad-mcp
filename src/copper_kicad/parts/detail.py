"""Full detail for one LCSC part: JLC assembly data (authoritative for PCBA),
LCSC retail data (labelled as retail — a different stock pool), parameters,
datasheet, EasyEDA symbol/footprint/3D availability and the KiCad library
route parts_import would take."""
from __future__ import annotations

from . import common as C
from .common import NetError

WMSC = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={code}"


def lcsc_retail(code: str, ttl: float = 3600) -> dict | None:
    hit = C.cache_get("wmsc", code, ttl)
    if hit is None:
        d = C.http_json(WMSC.format(code=code), headers={"User-Agent": C.UA_BROWSER}, retries=1, timeout=20)
        if not isinstance(d, dict) or d.get("code") != 200 or not d.get("result"):
            return None
        hit = d["result"]
        C.cache_put("wmsc", code, hit)
    r = hit
    return {
        "mfr": r.get("productModel"), "manufacturer": r.get("brandNameEn"), "package": r.get("encapStandard"),
        "category": r.get("parentCatalogName"), "subcategory": r.get("catalogName"),
        "description": r.get("productIntroEn") or r.get("productDescEn"),
        "retail_stock": r.get("stockNumber"), "min_buy": r.get("minBuyNumber"),
        "retail_price": [{"qty": p.get("ladder"), "usd": p.get("usdPrice")} for p in r.get("productPriceList") or []],
        "datasheet": (r.get("pdfUrl") or "").split("?")[0] or None,
        "params": {p.get("paramNameEn"): p.get("paramValueEn") for p in r.get("paramVOList") or [] if p.get("paramNameEn")},
        "url": f"https://www.lcsc.com/product-detail/{code}.html",
    }


def easyeda_summary(code: str) -> dict:
    from . import easyeda as E
    try:
        comp = E.fetch(code)
    except NetError as e:
        return {"available": False, "error": str(e)}
    ds = comp.get("dataStr") or {}
    pkg = comp.get("packageDetail") or {}
    pins = sum(1 for s in ds.get("shape") or [] if s.startswith("P~"))
    for sp in comp.get("subparts") or []:
        pins += sum(1 for s in (sp.get("dataStr") or {}).get("shape") or [] if s.startswith("P~"))
    pads = sum(1 for s in (pkg.get("dataStr") or {}).get("shape") or [] if s.startswith("PAD~"))
    has3d = any(s.startswith("SVGNODE") for s in (pkg.get("dataStr") or {}).get("shape") or [])
    cp = (ds.get("head") or {}).get("c_para") or {}
    return {"available": True, "symbol_pins": pins, "units": max(1, len(comp.get("subparts") or [])),
            "footprint": pkg.get("title"), "footprint_pads": pads, "has_3d": has3d,
            "jlc_class_easyeda": cp.get("JLCPCB Part Class"),
            "attribution": "JLCEDA/EasyEDA Official Library (https://lceda.cn/ , https://easyeda.com)"}


def get(lcsc: str, live: bool = True, easyeda: bool = True) -> dict:
    from . import basic, search, importer
    code = C.lcsc_norm(lcsc)
    if not code:
        raise ValueError(f"not an LCSC number: {lcsc!r}")
    out: dict = {"lcsc": code, "sources": {}}
    rec = None
    if live:
        try:
            rec = search.jlc_part(code)
            out["sources"]["jlc"] = "ok" if rec else "not listed by JLC (not assemblable at JLCPCB?)"
        except NetError as e:
            out["sources"]["jlc"] = f"unavailable: {e}"
    seed = basic.by_lcsc(code)
    if rec is None and seed:
        rec = seed
        out["sources"]["seed"] = f"snapshot {basic.snapshot_date()}"
    retail = None
    if live:
        try:
            retail = lcsc_retail(code)
            out["sources"]["lcsc"] = "ok" if retail else "not found"
        except NetError as e:
            out["sources"]["lcsc"] = f"unavailable: {e}"
    if rec is None and retail is None:
        out["found"] = False
        if easyeda and live:
            out["easyeda"] = easyeda_summary(code)
        return out
    out["found"] = True
    base = rec or {}
    out.update({
        "mfr_part": base.get("mfr") or (retail or {}).get("mfr"),
        "manufacturer": base.get("manufacturer") or (retail or {}).get("manufacturer"),
        "package": base.get("package") or (retail or {}).get("package"),
        "category": f"{base.get('category') or (retail or {}).get('category') or ''} / {base.get('subcategory') or (retail or {}).get('subcategory') or ''}",
        "description": base.get("description") or (retail or {}).get("description"),
        "datasheet": base.get("datasheet") or (retail or {}).get("datasheet"),
    })
    if rec:
        out["jlc_assembly"] = {
            "class": rec.get("class"), "basic": rec.get("basic"), "preferred": rec.get("preferred"),
            "stock": rec.get("stock"), "stock_source": rec.get("stock_source"),
            "price_usd": C.ladder_str(rec.get("price") or [], 8),
            "min_order": rec.get("min_order"), "attrition_extra_parts": rec.get("attrition"),
            "setup_fee_usd": 0 if (rec.get("basic") or rec.get("preferred")) else C.JLC_SETUP_FEE_USD,
            "note": "JLCPCB assembly (PCBA) stock — this is what matters for a JLC-assembled board",
        }
        out["params"] = {k: v for k, v in (rec.get("attributes") or {}).items()}
        if rec.get("typed"):
            out["typed"] = rec["typed"]
        out["kind"] = rec.get("kind")
    if retail:
        out["lcsc_retail"] = {"stock": retail["retail_stock"], "price_usd": C.ladder_str([p for p in retail["retail_price"] if p.get("usd") is not None], 8),
                              "min_buy": retail["min_buy"], "url": retail["url"],
                              "note": "LCSC retail stock (buying loose parts) — NOT the JLC assembly pool"}
        if not out.get("params"):
            out["params"] = retail["params"]
    # library route
    if rec:
        sc = importer.stock_candidate(rec)
        syms = importer.find_stock_symbols(rec.get("mfr") or "")
        out["kicad_library"] = {
            "stock_candidate": sc and {"lib_id": sc["lib_id"], "footprint": sc["footprint"]},
            "stock_symbols_named_like_mpn": syms[:4],
            "parts_import_would_use": ("kicad-stock if the pinout check against supplier data passes at import, else easyeda → project "
                                       "copper-lcsc libs") if (sc or syms) else
                                      "a generic KiCad symbol if the pinout fits (checked at import), else easyeda → project copper-lcsc libs",
        }
    if easyeda and live:
        out["easyeda"] = easyeda_summary(code)
    return out
