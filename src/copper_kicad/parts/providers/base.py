"""The parts-provider interface. A provider is where part data comes from: a distributor's
catalogue, a company stockroom file, KiCad's own libraries. Copper's tools only ever talk to this
interface, so adding a vendor never touches the tools. docs/PROVIDERS.md has a complete example.

A provider subclasses :class:`Provider` (or just looks like one) and implements ``search`` and ``get``.
Everything else has a working default.

The part record every method returns (a plain dict; extra keys are passed through to the caller)::

    provider      the provider's name                                   (filled in for you)
    id            the provider's own part number, a string              REQUIRED
    mpn           manufacturer part number                              vendor-neutral, always carried
    manufacturer  manufacturer name                                     vendor-neutral, always carried
    description   one line
    value         what goes in the schematic Value field ('10k', '100nF', 'AMS1117-3.3')
    package       package name ('0603', 'SOT-223')
    kind          resistor | capacitor | led | diode | ldo | header | ic | … (optional, helps matching)
    lib_id        KiCad symbol 'Library:Name'      } when the provider knows them; otherwise Copper
    footprint     KiCad footprint 'Library:Name'   } derives them (chip passives, MPN-named symbols)
    datasheet     URL
    stock         units in stock, or None when unknown
    price         unit price (number) or a ladder [{'qty': 1, 'usd': 0.01}, …]
    class         assembly class when the supplier has them (one of ``assembly_classes``)
    fields        extra schematic fields to stamp, e.g. {'Digi-Key': '296-1234-ND'}
    match         'exact' | 'partial' — how well it answers the query
"""
from __future__ import annotations

import re

from ...mcp.registry import ToolError

NEUTRAL_FIELDS = ("MPN", "Manufacturer")


class Provider:
    #: id used in config ("parts.providers") and in tool arguments (provider=…). Lower case, no spaces.
    name: str = ""
    #: human name for messages
    label: str = ""
    #: the schematic field that holds this provider's part number (None = it has no numbers of its own)
    part_field: str | None = None
    #: assembly price classes, cheapest first; () when the supplier has none
    assembly_classes: tuple = ()
    #: True when a part with only a Value and a Footprint counts as sourced (generic library parts)
    generic: bool = False
    #: True when search/get need the network
    needs_network: bool = False

    def __init__(self, options: dict | None = None, project_dir=None):
        self.options = dict(options or {})
        self.project_dir = project_dir

    # ── required ──────────────────────────────────────────────────────────
    def search(self, query: str, filters: dict | None = None, limit: int = 8, live: bool = True) -> dict:
        """→ {'results': [part records], 'sources': [...]} best first. filters: category, package,
        in_stock, min_stock, basic_only (cheapest assembly class only)."""
        raise NotImplementedError

    def get(self, part_id: str, live: bool = True) -> dict | None:
        """One part by this provider's id (or None)."""
        raise NotImplementedError

    # ── optional (defaults work) ──────────────────────────────────────────
    def owns(self, part_id: str) -> bool:
        """Does this id look like one of mine? Used when a tool gets a part id without provider=."""
        return False

    def pick(self, spec: str, live: bool = True) -> dict | None:
        """The single best part for a free-text spec ('10k 0603'), or None. Default: first exact search hit."""
        res = self.search(spec, {"in_stock": True}, limit=5, live=live).get("results") or []
        exact = [r for r in res if r.get("match", "exact") == "exact"]
        return (exact or [None])[0]

    def resolve(self, pr, part, **opts) -> dict:
        """Make a part placeable in project `pr`: → {'lib_id', 'footprint', 'value', 'fields', …}.
        `part` is an id or a record. The default uses the record's own lib_id/footprint, else KiCad's
        stock libraries (chip passives by package, symbols named after the MPN). Override to import
        symbols/footprints/3D models from the supplier."""
        rec = part if isinstance(part, dict) else self.get(str(part))
        if not rec:
            raise ToolError(f"{self.label or self.name}: part '{part}' not found", code="PART_NOT_FOUND",
                            fix=f"parts_search(query=..., provider='{self.name}')")
        from . import kicad_fit
        rec = self.normalize(rec) if isinstance(rec, dict) else {}
        if not rec.get("id"):
            raise ToolError(f"{self.label or self.name}: the record for part '{part}' has no id", code="PART_NOT_FOUND",
                            fix=f"parts_search(query=..., provider='{self.name}')")
        fit = kicad_fit.fit(rec, getattr(pr, "dir", None))
        if not fit.get("lib_id"):
            raise ToolError(f"{self.label or self.name}: no KiCad symbol known for {rec.get('id')} "
                            f"({rec.get('mpn') or rec.get('value') or '?'}, {rec.get('package') or 'no package'})",
                            code="NO_SYMBOL",
                            fix="give the part a symbol and footprint (catalogue columns 'symbol' and 'footprint'), or place "
                                "it with an explicit lib_id + footprint and set_field the part number")
        return {"provider": self.name, "id": rec["id"], "lib_id": fit["lib_id"], "footprint": fit.get("footprint") or "",
                "value": rec.get("value") or rec.get("mpn") or rec["id"], "fields": self.schematic_fields(rec),
                "class": rec.get("class"), "mpn": rec.get("mpn"), "package": rec.get("package"),
                "stock": rec.get("stock"), "source": fit.get("source", "kicad-stock"), "why": fit.get("why"),
                "warnings": list(fit.get("warnings") or []), "files_written": []}

    def match_component(self, comp: dict, policy: dict, live: bool = True) -> tuple[dict | None, str, list[dict]]:
        """BOM sourcing: the part for one schematic component → (record | None, reason, other options).
        comp: {'ref', 'value', 'footprint', 'lib_id', 'fields'}. Default: the MPN field, else Value + the
        footprint's package, through ``search``."""
        from .. import sourcing
        mpn = sourcing.existing_mpn(comp.get("fields") or {})
        spec = sourcing.infer_spec(comp)
        queries = []
        if mpn:
            queries.append((mpn, f"MPN field '{mpn}'"))
        q = sourcing._spec_query(spec)
        if q:
            queries.append((q, f"value + package '{q}'"))
        f = {"in_stock": bool(policy.get("require_in_stock", True))}
        for q, why in queries:
            res = [self.normalize(r) for r in (self.search(q, f, limit=4, live=live).get("results") or [])]
            exact = [r for r in res if r.get("match", "exact") == "exact"]
            if exact:
                return exact[0], why, exact[1:4]
            if res:
                return None, f"only partial matches for {why}", res[:3]
        return None, "no match in this provider", []

    def alternatives(self, target: str, limit: int = 8, same_package: bool = True, live: bool = True) -> dict:
        """Substitutes for a part id or a spec. Default: other search hits for the part's value + package."""
        rec = self.get(target, live=live) if self.owns(target) else None
        q = target
        if rec:
            rec = self.normalize(rec)
            q = " ".join(x for x in (rec.get("value") or rec.get("mpn"), rec.get("package") if same_package else "") if x)
        res = [self.normalize(r) for r in self.search(q, {"in_stock": True}, limit=limit + 1, live=live).get("results") or []]
        return {"target": target, "alternatives": [r for r in res if not rec or r["id"] != rec["id"]][:limit]}

    def status(self) -> dict:
        """Shown by copper_status / copper doctor."""
        return {"name": self.name, "label": self.label or self.name, "part_field": self.part_field,
                "needs_network": self.needs_network}

    def health(self) -> dict | None:
        """A cheap self-check for copper_status / project_summary: None when usable, else
        {'message', 'fix'} (a missing catalogue file, a missing API key …). Must not use the network."""
        return None

    # ── helpers ──────────────────────────────────────────────────────────
    def normalize(self, rec: dict) -> dict:
        """Fill the neutral keys of a record in place (id, provider, mpn, manufacturer)."""
        rec.setdefault("provider", self.name)
        rec["id"] = str(rec.get("id") or rec.get("mpn") or "").strip()
        rec.setdefault("mpn", "")
        rec.setdefault("manufacturer", "")
        return rec

    def schematic_fields(self, rec: dict) -> dict:
        """The fields stamped on a placed symbol: the provider's own part-number field (when it has one),
        ALWAYS the vendor-neutral MPN + Manufacturer when known, then Datasheet/Description and extras."""
        f = {}
        if self.part_field and rec.get("id") and self.part_field not in NEUTRAL_FIELDS:
            f[self.part_field] = rec["id"]
        if rec.get("mpn"):
            f["MPN"] = str(rec["mpn"])
        if rec.get("manufacturer"):
            f["Manufacturer"] = str(rec["manufacturer"])
        if rec.get("datasheet"):
            f["Datasheet"] = str(rec["datasheet"])
        if rec.get("description"):
            f["Description"] = str(rec["description"])[:250]
        for k, v in (rec.get("fields") or {}).items():
            if v not in (None, ""):
                f[str(k)] = str(v)
        return f


def check(obj) -> list[str]:
    """Why `obj` is not a usable provider (empty list = fine). Duck-typed: no base class required."""
    bad = []
    name = getattr(obj, "name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_\-]*", name or ""):
        bad.append("needs a `name` (lower-case letters, digits, '-' or '_')")
    for m in ("search", "get"):
        if not callable(getattr(obj, m, None)):
            bad.append(f"needs a `{m}` method")
    return bad
