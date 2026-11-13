"""The "kicad" provider: generic library parts with no supplier attached.

A part is just a KiCad symbol + footprint + Value. Nothing is looked up anywhere, nothing is locked to a
vendor, and a component that has a Value and a Footprint counts as sourced. Use it on its own for a
hand-assembled board, or after another provider (``["local", "kicad"]``) as the fallback for jelly-bean
parts your catalogue does not list.

  '10k 0603'            → Device:R + Resistor_SMD:R_0603_1608Metric, Value 10k
  'LED red 0805'        → Device:LED + LED_SMD:LED_0805_2012Metric
  'Header 1x4 2.54mm'   → Connector_Generic:Conn_01x04 + PinHeader_1x04_P2.54mm_Vertical
  'AMS1117-3.3'         → Regulator_Linear:AMS1117-3.3 with that symbol's default footprint
  anything else         → the KiCad symbol libraries are searched by name / keywords
"""
from __future__ import annotations

import re

from .base import Provider


class KicadProvider(Provider):
    name = "kicad"
    label = "KiCad libraries (generic parts)"
    part_field = None
    generic = True

    def owns(self, part_id) -> bool:
        s = str(part_id or "")
        return ":" in s and not re.match(r"^[a-z]+://", s)

    def _from_spec(self, query: str) -> dict | None:
        from .. import basic
        from . import kicad_fit
        got = kicad_fit.from_spec(basic.parse_spec(query))
        if not got:
            return None
        return {"provider": self.name, "id": got["lib_id"], "lib_id": got["lib_id"], "footprint": got["footprint"],
                "value": got.get("value") or query, "package": got.get("package") or "", "kind": got.get("kind"),
                "mpn": "", "manufacturer": "", "description": f"generic {got['why']}", "stock": None, "match": "exact"}

    def _from_symbol(self, lib_id: str, match: str = "exact", desc: str = "") -> dict | None:
        from .. import importer
        info = importer.stock_symbol(lib_id, self.project_dir)
        if info is None:
            return None
        return {"provider": self.name, "id": lib_id, "lib_id": lib_id, "footprint": info.get("footprint") or "",
                "value": lib_id.split(":", 1)[1], "mpn": "", "manufacturer": "",
                "description": desc or info.get("description") or "", "datasheet": info.get("datasheet") or "",
                "pins": len(info.get("pins") or {}), "stock": None, "match": match}

    def search(self, query, filters=None, limit=8, live=True) -> dict:
        from ...kicad import libs
        from .. import basic, importer
        q = str(query or "").strip()
        f = dict(filters or {})
        if f.get("package") and f["package"].lower() not in q.lower():
            q = f"{q} {f['package']}"
        out, seen = [], set()

        def add(rec):
            if rec and rec["id"] + "|" + rec.get("footprint", "") not in seen:
                seen.add(rec["id"] + "|" + rec.get("footprint", ""))
                out.append(rec)

        add(self._from_spec(q))
        sp = basic.parse_spec(q)
        name = sp.get("mpn") or str(query or "").strip()
        for lid in importer.find_stock_symbols(name)[:limit]:
            add(self._from_symbol(lid))
        if not out and name:          # a spec / MPN-named symbol answered: keyword hits would only be noise
            try:
                for r in libs.search_symbols(name, self.project_dir, limit):
                    exact = r["lib_id"].split(":", 1)[1].lower() == name.lower()
                    add({"provider": self.name, "id": r["lib_id"], "lib_id": r["lib_id"],
                         "footprint": r.get("footprint") or r.get("default_footprint") or "",
                         "value": r["lib_id"].split(":", 1)[1], "mpn": "", "manufacturer": "",
                         "description": r.get("description") or "", "stock": None,
                         "match": "exact" if exact else "partial"})
            except Exception:  # noqa: BLE001 — a broken library table must not break a parts search
                pass
        return {"results": out[:max(1, limit)], "sources": {"kicad": "ok (KiCad symbol/footprint libraries; no supplier data)"},
                "note": "generic parts: value + footprint only, no supplier part number"}

    def get(self, part_id, live=True) -> dict | None:
        s = str(part_id or "").strip()
        if ":" in s:
            return self._from_symbol(s)
        return self.pick(s)

    def pick(self, spec, live=True) -> dict | None:
        from .. import basic, importer
        got = self._from_spec(spec)
        if got:
            return got
        sp = basic.parse_spec(spec)
        name = sp.get("mpn") or (str(spec).strip() if re.fullmatch(r"[\w.+\-/#]{3,}", str(spec).strip()) else "")
        for lid in importer.find_stock_symbols(name or ""):
            rec = self._from_symbol(lid)
            if rec:
                return rec
        return None

    def resolve(self, pr, part, **opts) -> dict:
        rec = part if isinstance(part, dict) else self.get(str(part))
        if not rec:
            from ...mcp.registry import ToolError
            raise ToolError(f"no KiCad library part for '{part}'", code="PART_NOT_FOUND",
                            fix="library(action='search_symbols', query=...) and place with lib_id + footprint")
        warnings = []
        if not rec.get("footprint"):
            warnings.append(f"{rec['lib_id']} has no default footprint — set one (set_field Footprint) before the PCB")
        return {"provider": self.name, "id": rec["id"], "lib_id": rec["lib_id"], "footprint": rec.get("footprint") or "",
                "value": rec.get("value") or rec["id"], "fields": self.schematic_fields(rec), "class": None,
                "mpn": "", "package": rec.get("package"), "stock": None, "source": "kicad-stock",
                "why": rec.get("description"), "warnings": warnings, "files_written": []}

    def schematic_fields(self, rec: dict) -> dict:
        f = {}
        if rec.get("datasheet") and rec["datasheet"] != "~":
            f["Datasheet"] = rec["datasheet"]
        return f

    def match_component(self, comp, policy, live=True):
        if comp.get("value") and comp.get("footprint"):
            return ({"provider": self.name, "id": "", "mpn": "", "manufacturer": "", "generic": True, "match": "exact"},
                    "generic part: value + footprint, no supplier number", [])
        return None, "needs a Value and a Footprint", []

    def status(self) -> dict:
        d = super().status()
        d["note"] = "value + footprint only; never needs the network"
        return d
