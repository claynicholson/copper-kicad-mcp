"""A complete Copper parts provider in one file. Copy it, rename it, point it at your own data.

It models a small in-house stockroom: every part has a stock number ("SR-0001"), which Copper writes into
the schematic field "Stock No" next to the vendor-neutral MPN and Manufacturer.

Use it (no install needed) by naming this file in the Copper config, $COPPER_HOME/config.json:

    {"parts": {"providers": ["stockroom", "kicad"],
               "provider_paths": ["C:/path/to/example_provider.py"],
               "options": {"stockroom": {"min_stock": 1}}}}

then:  parts_search(query="10k 0603")  ·  sch_apply ops=[{"op":"place","spec":"10k 0603"}]
       sch_apply ops=[{"op":"place","part":"SR-0003"}]  ·  parts_source_bom()

This file is exercised by tests/test_neutral.py, so it always matches the interface.
A real provider would replace PARTS with a database query or an HTTP call inside search() and get().
"""
from copper_kicad.parts import common as C
from copper_kicad.parts.providers import Provider

# In real life: your ERP, a spreadsheet export, a distributor API. Keys are the part-record keys
# documented in copper_kicad/parts/providers/base.py; only "id" is required.
PARTS = [
    {"id": "SR-0001", "mpn": "RC0603FR-0710KL", "manufacturer": "Yageo", "value": "10k", "package": "0603",
     "kind": "resistor", "stock": 4200, "price": 0.004, "fields": {"Shelf": "A1"}},
    {"id": "SR-0002", "mpn": "CL10B104KB8NNNC", "manufacturer": "Samsung", "value": "100nF", "package": "0603",
     "kind": "capacitor", "stock": 900, "price": 0.006, "fields": {"Shelf": "A2"}},
    {"id": "SR-0003", "mpn": "AP2112K-3.3TRG1", "manufacturer": "Diodes Inc.", "value": "AP2112K-3.3",
     "package": "SOT-23-5", "kind": "ldo", "stock": 35, "price": 0.21,
     # a part whose symbol/footprint Copper cannot guess: name them (any KiCad library the machine has)
     "lib_id": "Regulator_Linear:AP2112K-3.3", "footprint": "Package_TO_SOT_SMD:SOT-23-5",
     "datasheet": "https://www.diodes.com/assets/Datasheets/AP2112.pdf", "fields": {"Shelf": "C4"}},
    {"id": "SR-0004", "mpn": "150060RS75000", "manufacturer": "Wurth", "value": "LED red", "package": "0603",
     "kind": "led", "stock": 0, "price": 0.09, "fields": {"Shelf": "B7"}},
]


class StockroomProvider(Provider):
    name = "stockroom"              # the name used in config and in provider=…
    label = "in-house stockroom"
    part_field = "Stock No"         # schematic field that receives the part's id

    # ── required: search and get ─────────────────────────────────────────
    def search(self, query, filters=None, limit=8, live=True):
        """Best matches first. `match` is 'exact' when every term of the query is satisfied."""
        from copper_kicad.parts import basic
        filters = filters or {}
        spec = basic.parse_spec(query)             # '10k 0603' → {'kind': 'resistor', 'value': 10000.0, 'package': '0603', …}
        words = [w for w in str(query).lower().split() if len(w) > 1]
        min_stock = int(self.options.get("min_stock", 0))
        hits = []
        for p in PARTS:
            if filters.get("in_stock") and p["stock"] <= max(0, min_stock - 1):
                continue
            if filters.get("package") and C.norm_pkg(p["package"]) != C.norm_pkg(filters["package"]):
                continue
            score = self._score(p, spec, words)
            if score is not None:
                hits.append((score, -p["stock"], p))
        hits.sort(key=lambda h: h[:2])
        results = [dict(p, match="exact" if s == 0 else "partial") for s, _, p in hits[:limit]]
        return {"results": results, "sources": {"stockroom": f"ok ({len(PARTS)} parts)"}}

    def get(self, part_id, live=True):
        key = str(part_id).strip().upper()
        return next((dict(p) for p in PARTS if key in (p["id"].upper(), p["mpn"].upper())), None)

    # ── optional ─────────────────────────────────────────────────────────
    def owns(self, part_id):
        """Lets tools find this provider from a bare id: parts_get(part='SR-0003')."""
        return str(part_id).strip().upper().startswith("SR-")

    # pick(), resolve(), match_component(), alternatives() and status() are inherited:
    #   pick(spec)            the first exact search hit
    #   resolve(pr, part)     the record's own lib_id/footprint, else KiCad stock parts (Device:R + R_0603… for a
    #                         0603 resistor, the symbol named after the MPN for ICs)
    #   match_component(...)  BOM sourcing by the MPN field, then by Value + footprint package
    # Override resolve() if you can import symbols/footprints from your supplier.

    @staticmethod
    def _score(p, spec, words):
        """0 = exact, 1 = partial, None = no match."""
        if spec.get("mpn") and spec["mpn"].upper() in (p["mpn"].upper(), p["id"].upper(), str(p["value"]).upper()):
            return 0
        if spec.get("value") is not None and spec.get("unit"):
            if spec.get("kind") and spec["kind"] != p["kind"]:
                return None
            if not C.close(C.parse_si(p["value"], spec["unit"]), spec["value"]):
                return None
            if spec.get("package") and C.norm_pkg(p["package"]) != C.norm_pkg(spec["package"]):
                return None
            return 0
        text = " ".join(str(p.get(k, "")) for k in ("id", "mpn", "manufacturer", "value", "package", "kind")).lower()
        return 0 if words and all(w in text for w in words) else None


# Copper looks for PROVIDER (or PROVIDERS = [...]) first; without it, every provider class in the module is used.
PROVIDER = StockroomProvider
