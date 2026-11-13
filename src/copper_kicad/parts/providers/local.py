"""The "local" provider: your own parts catalogue in a CSV or JSON file. Fully offline.

This is how a Digi-Key / Mouser / in-house-stockroom shop uses Copper without writing code: export
the parts you buy to a spreadsheet, save it as CSV, point Copper at it.

Where the file lives (first that exists; several are merged, earlier wins on duplicate ids)::

    config  parts.catalogue = "stockroom.csv" | ["a.csv", "b.json"]     (or parts.options.local.catalogue)
    <project>/.copper/parts.csv | parts.json
    $COPPER_HOME/parts.csv | parts.json

Columns (CSV header row, any order, case-insensitive; JSON objects use the same keys)::

    mpn           manufacturer part number                     } at least one of mpn / value
    value         '10k', '100nF', 'LED red' …                  }
    manufacturer  maker
    package       '0603', 'SOT-223' …
    symbol        KiCad symbol   'Device:R'                    } optional: chip passives, pin headers and
    footprint     KiCad footprint 'Resistor_SMD:R_0603_1608Metric'   } MPN-named KiCad symbols are derived
    id            your own stock number (default: the mpn)
    kind, description, datasheet, stock, price
    <anything else>   becomes a schematic field of that name: 'Digi-Key', 'Mouser', 'LCSC', 'Bin' …

A JSON catalogue is a list of such objects, or {"parts": [...]}; an object may also carry
"fields": {"Digi-Key": "..."}.
"""
from __future__ import annotations

import codecs
import csv
import io
import json
import re
from pathlib import Path

from ...mcp.registry import ToolError
from .. import common as C
from .base import Provider

_ALIASES = {
    "id": ("id", "part", "part id", "part_id", "stock number", "stock no", "internal pn", "ipn", "sku"),
    "mpn": ("mpn", "manufacturer part number", "manufacturer_part_number", "mfr part", "mfr part #", "mfr. part #",
            "mfg part #", "mfg part", "manufacturer part", "part number", "partnumber"),
    "manufacturer": ("manufacturer", "mfr", "mfg", "maker", "brand"),
    "value": ("value", "val", "comment"),
    "package": ("package", "pkg", "case", "case/package"),
    "kind": ("kind", "type", "category"),
    "description": ("description", "desc"),
    "lib_id": ("symbol", "lib_id", "kicad symbol", "kicad_symbol", "symbol lib_id"),
    "footprint": ("footprint", "kicad footprint", "kicad_footprint"),
    "datasheet": ("datasheet", "datasheet url"),
    "stock": ("stock", "qty", "quantity", "on hand", "in stock", "qty on hand"),
    "price": ("price", "unit price", "price_usd", "cost", "unit cost"),
}
_KEY = {a: k for k, al in _ALIASES.items() for a in al}
HEADER_EXAMPLE = "mpn,manufacturer,value,package,symbol,footprint,stock,Digi-Key"
_CACHE: dict = {}


def _alnum(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _bad(path: Path, msg: str, fix: str, line: int | None = None) -> ToolError:
    return ToolError(f"local parts catalogue {path}" + (f", line {line}" if line else "") + f": {msg}",
                     code="BAD_CATALOGUE", fix=fix, data={"file": str(path), **({"line": line} if line else {})})


def _row(raw: dict, path: Path, line: int) -> dict | None:
    rec: dict = {"fields": {}}
    for k, v in raw.items():
        if k is None:
            raise _bad(path, "more values than header columns", "quote values that contain commas", line)
        if isinstance(v, dict) and str(k).strip().lower() == "fields":
            rec["fields"].update({str(a): str(b) for a, b in v.items() if b not in (None, "")})
            continue
        val = "" if v is None else str(v).strip()
        key = _KEY.get(str(k).strip().lower())
        if key:
            rec[key] = val
        elif val and str(k).strip() and not str(k).startswith(("_", "#")):
            rec["fields"][str(k).strip()] = val
    if not any(rec.get(k) for k in ("mpn", "value", "id")):
        return None                                   # blank line
    if rec.get("stock") not in (None, ""):
        try:
            rec["stock"] = int(float(str(rec["stock"]).replace(",", "")))
        except ValueError:
            raise _bad(path, f"stock '{rec['stock']}' is not a number", "use a whole number, or leave the cell empty", line)
    else:
        rec["stock"] = None
    if rec.get("price") not in (None, ""):
        try:
            rec["price"] = float(str(rec["price"]).replace("$", "").replace(",", "."))
        except ValueError:
            raise _bad(path, f"price '{rec['price']}' is not a number", "use a plain number such as 0.012", line)
    else:
        rec["price"] = None
    rec["id"] = rec.get("id") or rec.get("mpn") or " ".join(x for x in (rec.get("value"), rec.get("package")) if x)
    rec["mpn"] = rec.get("mpn") or ""
    rec["manufacturer"] = rec.get("manufacturer") or ""
    rec["value"] = rec.get("value") or rec["mpn"]
    for k in ("lib_id", "footprint"):
        if rec.get(k) and ":" not in rec[k]:
            raise _bad(path, f"{'symbol' if k == 'lib_id' else 'footprint'} '{rec[k]}' must be 'Library:Name'",
                       "e.g. symbol Device:R, footprint Resistor_SMD:R_0603_1608Metric "
                       "(find them with library(action='search_symbols' | 'search_footprints'))", line)
    rec["_line"] = line
    rec["_file"] = path.name
    return rec


def read_catalogue(path: Path) -> list[dict]:
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        raise _bad(path, "file not found", f"create it (CSV header: {HEADER_EXAMPLE}) or fix parts.catalogue in the config")
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise _bad(path, f"cannot be read ({e})", "make the file readable")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        if path.suffix.lower() == ".json":           # JSON is UTF-8 by definition
            raise _bad(path, f"is not UTF-8 ({e})", "save it as UTF-8 JSON")
        # a spreadsheet's plain "CSV" export on Windows is the ANSI code page, not UTF-8
        text = raw.decode("utf-16") if raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE) else raw.decode("cp1252", errors="replace")
    rows: list[dict] = []
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise _bad(path, e.msg, "fix the JSON syntax there", e.lineno)
        items = data.get("parts") if isinstance(data, dict) else data
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise _bad(path, "expected a list of part objects (or {\"parts\": [...]})",
                       '[{"mpn": "RC0603FR-0710KL", "value": "10k", "package": "0603"}]', 1)
        for i, it in enumerate(items, 1):
            r = _row(it, path, i)
            if r:
                rows.append(r)
    else:
        sample = text[:4096]
        delim = ";" if sample.count(";") > sample.count(",") and "\t" not in sample else ("\t" if sample.count("\t") > sample.count(",") else ",")
        rd = csv.DictReader(io.StringIO(text), delimiter=delim)
        heads = [str(h or "").strip().lower() for h in (rd.fieldnames or [])]
        if not any(_KEY.get(h) in ("mpn", "value", "id") for h in heads):
            raise _bad(path, f"the header row has none of the columns mpn / value / id (found: {rd.fieldnames})",
                       f"make the first line a header such as: {HEADER_EXAMPLE}", 1)
        try:
            for raw in rd:
                r = _row(raw, path, rd.line_num)
                if r:
                    rows.append(r)
        except csv.Error as e:
            raise _bad(path, str(e), "fix the quoting on that line", rd.line_num)
    _CACHE.clear() if len(_CACHE) > 16 else None
    _CACHE[key] = rows
    return rows


class LocalProvider(Provider):
    name = "local"
    label = "local catalogue"
    part_field = "MPN"

    def __init__(self, options=None, project_dir=None):
        super().__init__(options, project_dir)
        if self.options.get("part_field"):
            self.part_field = str(self.options["part_field"])

    # ── catalogue ────────────────────────────────────────────────────────
    def catalogue_files(self) -> list[Path]:
        from ... import config
        cat = self.options.get("catalogue")
        if cat:
            return [Path(p) for p in ([cat] if isinstance(cat, str) else cat)]
        out = []
        for base in ([Path(self.project_dir) / ".copper"] if self.project_dir else []) + [config.home()]:
            for n in ("parts.csv", "parts.json"):
                if (base / n).is_file():
                    out.append(base / n)
        return out

    def rows(self) -> list[dict]:
        files = self.catalogue_files()
        if not files:
            from ... import config
            where = (f"{Path(self.project_dir) / '.copper' / 'parts.csv'} or " if self.project_dir else "") + \
                str(config.home() / "parts.csv")
            raise ToolError("the 'local' parts provider has no catalogue file", code="NO_CATALOGUE",
                            fix=f"create {where} (CSV header: {HEADER_EXAMPLE}), or set parts.catalogue in the Copper config "
                                f"(docs/PROVIDERS.md)")
        seen, out = set(), []
        for f in files:
            for r in read_catalogue(f):
                k = r["id"].lower()
                if k not in seen:
                    seen.add(k)
                    out.append(r)
        return out

    def _public(self, r: dict, match: str = "exact") -> dict:
        from . import kicad_fit
        d = {k: v for k, v in r.items() if not k.startswith("_") and v not in (None, "", {})}
        d.update(provider=self.name, id=r["id"], mpn=r["mpn"], manufacturer=r["manufacturer"], stock=r.get("stock"),
                 match=match, kind=kicad_fit.kind_of(r) or None)
        return d

    # ── matching ─────────────────────────────────────────────────────────
    def _score(self, r: dict, spec: dict, tokens: list[str]) -> int | None:
        from .. import basic
        from . import kicad_fit
        pkg = spec.get("package")
        pkg_ok = (not pkg) or (bool(r.get("package")) and basic.pkg_match(r["package"], pkg))
        if spec.get("mpn"):
            a = _alnum(spec["mpn"])
            cands = [_alnum(r["mpn"]), _alnum(r["id"]), _alnum(r["value"])]
            if a and a in cands:
                return 0 if pkg_ok else None
            if a and len(a) >= 4 and any(c and (c.startswith(a) or a.startswith(c)) and len(c) >= 4 for c in cands):
                return 1 if pkg_ok else None
        kind = kicad_fit.kind_of(r)
        if spec.get("value") is not None and spec.get("unit"):
            same = {"capacitor", "electrolytic", "tantalum"}
            if kind and spec.get("kind") and kind != spec["kind"] and not (kind in same and spec["kind"] in same):
                return None
            rv = C.parse_si(str(r.get("value") or ""), spec["unit"])
            if rv is None or not C.close(rv, spec["value"]):
                return None
            if not pkg_ok:
                return None if r.get("package") else 1
            return 0
        if spec.get("kind") == "led" and kind == "led":
            col = spec.get("color")
            hay = f"{r.get('value')} {r.get('description') or ''}".lower()
            if col and col not in hay:
                return None
            return 0 if pkg_ok else (None if r.get("package") else 1)
        if not tokens:
            return None
        hay = " ".join(str(r.get(k) or "") for k in ("id", "mpn", "value", "package", "description", "manufacturer", "kind")).lower()
        hay += " " + " ".join(r.get("fields", {}).values()).lower()
        if all(t in hay for t in tokens):
            return 0 if pkg_ok else 1
        return None

    def search(self, query, filters=None, limit=8, live=True) -> dict:
        from .. import basic
        f = dict(filters or {})
        q = str(query or "").strip()
        spec = basic.parse_spec(f"{q} {f['package']}" if f.get("package") and f["package"].lower() not in q.lower() else q)
        tokens = [t for t in re.split(r"[\s,;]+", q.lower()) if len(t) > 1]
        cat = str(f.get("category") or "").strip().lower()
        rows = self.rows()
        hits = []
        for r in rows:
            s = self._score(r, spec, tokens)
            if s is None:
                continue
            if cat:
                from . import kicad_fit
                if cat not in (kicad_fit.kind_of(r), str(r.get("kind") or "").lower()):
                    continue
            st = r.get("stock")
            if f.get("in_stock") and st is not None and st <= 0:
                continue
            if f.get("min_stock") and st is not None and st < int(f["min_stock"]):
                continue
            hits.append((s, -(st or 0), r["_line"], r))
        hits.sort(key=lambda t: t[:3])
        return {"results": [self._public(r, "exact" if s == 0 else "partial") for s, _, _, r in hits[:max(1, limit)]],
                "sources": {"local": f"ok ({len(rows)} parts in {', '.join(p.name for p in self.catalogue_files())})"},
                "total_considered": len(rows)}

    def get(self, part_id, live=True) -> dict | None:
        a = _alnum(part_id)
        for r in self.rows():
            if a and a in (_alnum(r["id"]), _alnum(r["mpn"])):
                return self._public(r)
        return None

    def owns(self, part_id) -> bool:
        try:
            return self.get(part_id) is not None
        except ToolError:
            return False

    def match_component(self, comp, policy, live=True):
        fp = str(comp.get("footprint") or "")
        rec, why, others = super().match_component(comp, policy, live)
        if rec and rec.get("footprint") and fp and rec["footprint"] != fp:
            # the catalogue names a different footprint than the schematic: do not silently mis-assign
            from .. import basic, sourcing
            pk = sourcing.footprint_package(fp.split(":")[-1], rec.get("kind"))
            if not (pk and rec.get("package") and basic.pkg_match(rec["package"], pk)):
                return None, (f"catalogue part {rec['id']} is drawn on {rec['footprint']} but the schematic uses {fp}"), [rec]
        return rec, why, others

    def health(self) -> dict | None:
        try:
            self.rows()
        except ToolError as e:
            return {"message": e.message, "fix": e.fix}
        return None

    def status(self) -> dict:
        d = super().status()
        try:
            files = self.catalogue_files()
            d["catalogue"] = [str(f) for f in files]
            d["parts"] = len(self.rows()) if files else 0
            if not files:
                d["note"] = f"no catalogue file yet (CSV header: {HEADER_EXAMPLE})"
        except ToolError as e:
            d["error"] = e.message
            d["fix"] = e.fix
        return d
