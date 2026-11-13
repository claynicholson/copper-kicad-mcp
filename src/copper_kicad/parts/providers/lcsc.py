"""The default provider: LCSC parts for JLCPCB assembly.

Wraps the existing engine unchanged — the bundled offline Basic/Preferred table (parts.basic), the live
JLC parts API and jlcsearch (parts.search), part detail (parts.detail), EasyEDA symbol / footprint / 3D
import (parts.importer + parts.easyeda) and BOM sourcing (parts.sourcing). Every vendor-specific name in
the parts engine lives behind this class.
"""
from __future__ import annotations

import re

from ...mcp.registry import ToolError
from .. import common as C
from .base import Provider


class LcscProvider(Provider):
    name = "lcsc"
    label = "LCSC / JLCPCB"
    part_field = "LCSC"
    assembly_classes = ("Basic", "Preferred", "Extended")
    needs_network = False          # the offline table answers common parts; live data improves stock/class

    # ── records ──────────────────────────────────────────────────────────
    def normalize(self, rec: dict) -> dict:
        rec.setdefault("provider", self.name)
        rec["id"] = str(rec.get("id") or rec.get("lcsc") or "")
        rec["mpn"] = rec.get("mpn") or rec.get("mfr_part") or rec.get("mfr") or ""
        rec["manufacturer"] = rec.get("manufacturer") or ""
        return rec

    def owns(self, part_id: str) -> bool:
        return bool(re.fullmatch(r"\s*[Cc]\d{2,10}\s*", str(part_id or "")))

    # ── search / get / pick ──────────────────────────────────────────────
    def search(self, query, filters=None, limit=8, live=True) -> dict:
        from .. import search
        # expand: a description ('alcohol gas sensor', 'wifi module') is asked in the catalogue's wording and by its
        # category, and out-of-stock matches for a part number are listed apart from the results
        r = search.search(query, dict(filters or {}), limit=limit, live=live, expand=True)
        for x in r["results"] + (r.get("out_of_stock") or []):
            self.normalize(x)
        return r

    def get(self, part_id, live=True, easyeda=False) -> dict | None:
        from .. import detail
        code = C.lcsc_norm(part_id)
        if not code:
            return None
        d = detail.get(code, live=live, easyeda=easyeda)
        if not d.get("found"):
            return None
        ja = d.get("jlc_assembly") or {}
        d.update(id=code, provider=self.name, mpn=d.get("mfr_part") or "", stock=ja.get("stock"),
                 **({"class": ja["class"]} if ja.get("class") else {}))
        d["manufacturer"] = d.get("manufacturer") or ""
        return d

    def detail(self, part_id, live=True, easyeda=True) -> dict:
        """The full parts_get payload (found=False included)."""
        from .. import detail
        d = detail.get(C.lcsc_norm(part_id), live=live, easyeda=easyeda)
        d.update(id=d.get("lcsc"), provider=self.name, mpn=d.get("mfr_part") or "")
        return d

    def pick(self, spec, live=True) -> dict | None:
        from .. import basic, search
        sp = basic.parse_spec(spec)
        if not (sp.get("kind") or sp.get("mpn") or sp.get("value") is not None) and sp.get("keywords"):
            # a description ('alcohol gas sensor') is not a spec: auto-pick only an in-stock part whose catalogue
            # entry confirms every word, never a part that merely shares one of them
            try:
                recs = search.search(spec, {"in_stock": True}, limit=4, live=live, raw=True, expand=live).get("records") or []
            except C.NetError:
                recs = []
            recs = [r for r in recs if r.get("_score", 9) == 0 and (r.get("stock") or 0) > 0]
            p = basic._picked(recs[0], sp, recs[1:], "live JLC search" if recs[0].get("source") == "jlc" else "offline table") \
                if recs else None
        else:
            p = basic.pick(spec, live=live)
        return self.normalize(p) if p else None

    # ── symbol / footprint / 3D ──────────────────────────────────────────
    def resolve(self, pr, part, prefer="auto", with_3d=True, live=True, fresh=False, **_) -> dict:
        from .. import importer
        code = part.get("id") or part.get("lcsc") if isinstance(part, dict) else part
        r = importer.resolve_lcsc(pr, str(code), prefer=prefer, with_3d=with_3d, live=live, fresh=fresh)
        r.update(provider=self.name, id=r.get("lcsc"))
        return r

    # ── sourcing ─────────────────────────────────────────────────────────
    def match_component(self, comp, policy, live=True):
        from .. import sourcing
        pol = dict(sourcing.DEFAULT_POLICY)
        pol.update({k: policy[k] for k in ("allow_extended", "min_stock", "basic_only") if k in policy})
        rec, why, others, _notes = sourcing._choose(sourcing.infer_spec(comp), pol, live)
        if rec is None:
            return None, why, [self.normalize(dict(o)) for o in others[:3]]
        return self._neutral(rec), why, [self._neutral(o) for o in others[:3]]

    def _neutral(self, rec: dict) -> dict:
        return {"provider": self.name, "id": rec.get("lcsc"), "lcsc": rec.get("lcsc"), "mpn": rec.get("mfr") or rec.get("mfr_part") or "",
                "manufacturer": rec.get("manufacturer") or "", "package": rec.get("package"), "class": rec.get("class"),
                "stock": rec.get("stock"), "price": rec.get("price"), "kind": rec.get("kind"),
                "description": rec.get("description") or "", "match": "exact"}

    def source_bom(self, pr, policy: dict) -> dict:
        """The LCSC-only sourcing pass (THT geometry, MPN families, live stock, class fees)."""
        from .. import sourcing
        return sourcing.source_bom(pr, policy)

    def alternatives(self, target, limit=8, same_package=True, live=True) -> dict:
        from .. import sourcing
        r = sourcing.alternatives(target, limit=limit, live=live, same_package=same_package)
        for x in r.get("alternatives") or []:
            self.normalize(x)
        return r

    def schematic_fields(self, rec: dict) -> dict:
        f = super().schematic_fields(self.normalize(rec))
        if rec.get("class"):
            f["JLC Class"] = rec["class"]
        return f

    def status(self) -> dict:
        from .. import basic
        d = super().status()
        try:
            d["offline_table"] = f"{len(basic.all_parts())} parts, snapshot {basic.snapshot_date()}"
        except Exception as e:  # noqa: BLE001
            d["offline_table"] = f"unavailable ({e})"
        d["imports"] = "symbol + footprint + 3D (KiCad stock when it fits, else EasyEDA)"
        return d


def require_code(part_id) -> str:
    code = C.lcsc_norm(part_id)
    if not code:
        raise ToolError(f"'{part_id}' is not an LCSC part number", code="BAD_ARGUMENT", fix="pass e.g. part='C25804'")
    return code
