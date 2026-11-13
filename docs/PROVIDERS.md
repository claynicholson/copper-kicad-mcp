# Parts providers

A parts provider is where part data comes from. Copper's tools (`parts_search`, `parts_get`, `parts_import`,
`parts_source_bom`, `parts_alternatives`, and `sch_apply` when it places by `spec` or `part`) only talk to the
provider interface, so a new vendor never needs a change in Copper.

Which providers are active, and in what order, is `parts.providers` in the Copper config
([CONFIG.md](CONFIG.md)); every tool also takes `provider=` to ask one directly, and every result says which
provider answered.

## Built-in providers

| Name | What it does | Part-number field | Network |
|---|---|---|---|
| `lcsc` (default) | LCSC parts for JLCPCB assembly. Offline table of JLC Basic + Preferred parts, live JLC assembly stock and prices, jlcsearch fallback, EasyEDA symbol / footprint / 3D import, THT-aware BOM sourcing, Basic > Preferred > Extended ranking. | `LCSC` (+ `JLC Class`) | optional (offline table answers common parts) |
| `local` | Your own catalogue file. | `MPN` (configurable) | never |
| `kicad` | Generic KiCad library parts: a symbol, a footprint and a Value. A part with a Value and a Footprint counts as sourced. | none | never |

Whatever the provider, a placed part also gets the vendor-neutral `MPN` and `Manufacturer` fields when the provider
knows them. A design sourced from LCSC can therefore be re-sourced elsewhere later: its BOM still says what each
part *is*.

### `local`: a catalogue file

This is how a Digi-Key / Mouser / in-house-stockroom shop uses Copper without writing code.

Copper looks for the file in this order: `parts.catalogue` in the config (a path or a list),
`<project>/.copper/parts.csv` / `parts.json`, `$COPPER_HOME/parts.csv` / `parts.json`.

```csv
mpn,manufacturer,value,package,symbol,footprint,stock,Digi-Key,Bin
RC0603FR-0710KL,Yageo,10k,0603,,,5000,311-10.0KHRCT-ND,A3
CL21A106KAYNNNE,Samsung,10uF,0805,,,750,1276-1096-1-ND,B1
AMS1117-3.3,Advanced Monolithic Systems,AMS1117-3.3,SOT-223,Regulator_Linear:AMS1117-3.3,Package_TO_SOT_SMD:SOT-223-3_TabPin2,40,,C7
```

| Column | Meaning |
|---|---|
| `mpn`, `value` | at least one per row. `value` is what goes in the schematic (`10k`, `100nF`, `LED red`). |
| `manufacturer`, `package`, `description`, `datasheet`, `kind` | optional. `package` (`0603`, `SOT-223`) is what lets `'10k 0603'` find the row. |
| `symbol`, `footprint` | optional KiCad `Library:Name`. Leave empty for chip passives, LEDs, diodes and pin headers (derived from kind + package) and for ICs KiCad has a symbol named after the MPN. |
| `id` | your own stock number; defaults to the MPN. |
| `stock`, `price` | optional numbers. Out-of-stock rows are skipped when `require_in_stock` is on. |
| any other column | becomes a schematic field of that name: `Digi-Key`, `Mouser`, `LCSC`, `Bin`, … |

Header names are case-insensitive and common spellings work (`Manufacturer Part Number`, `Mfr`, `Qty`, `Symbol`).
A CSV may be UTF-8 (with or without BOM), UTF-16, or a spreadsheet's ANSI export; commas, semicolons and tabs are
all accepted as the separator.
A JSON catalogue is a list of objects with the same keys, or `{"parts": [...]}`. Options
(`parts.options.local`): `catalogue`, `part_field` (default `MPN`; set it to `Digi-Key` to make that the number
fab BOMs carry). Errors name the file and line (`BAD_CATALOGUE`).

### `kicad`: generic parts

`'10k 0603'` → `Device:R` + `Resistor_SMD:R_0603_1608Metric`; `'LED red 0805'` → `Device:LED`;
`'Header 1x4 2.54mm'` → `Connector_Generic:Conn_01x04` + `PinHeader_1x04_P2.54mm_Vertical`;
`'AMS1117-3.3'` → the KiCad symbol of that name with its default footprint. Put it last in the list
(`["local", "kicad"]`) as the fallback for jelly-bean parts.

## Writing a provider

A provider is a class with a `name`, a `search` and a `get`. Subclass `copper_kicad.parts.providers.Provider` and
every other method has a working default.

```python
from copper_kicad.parts.providers import Provider

class MyProvider(Provider):
    name = "myshop"            # used in config and in provider=…   (lower case, digits, - or _)
    label = "My Shop"          # for messages
    part_field = "MyShop PN"   # schematic field that receives a part's id; None = no number of its own

    def search(self, query, filters=None, limit=8, live=True):
        # → {"results": [part records, best first], "sources": {...}}
        ...

    def get(self, part_id, live=True):
        # → one part record, or None
        ...
```

`docs/examples/example_provider.py` is a complete, runnable provider (an in-house stockroom) in about 100 lines.
`tests/test_neutral.py` loads that exact file through a config path, searches it, places parts from it with
`sch_apply`, sources a BOM with it and exports a fab pack, so the example cannot rot.

### The part record

A plain dict. Extra keys pass through to the caller.

| Key | Meaning |
|---|---|
| `id` | **required**: the provider's own part number (a string) |
| `mpn`, `manufacturer` | vendor-neutral identity; always carried into the schematic when present |
| `value` | schematic Value (`10k`, `100nF`, `AMS1117-3.3`) |
| `package`, `kind`, `description`, `datasheet` | `kind` is one of resistor, capacitor, led, diode, ldo, header, ic, … |
| `lib_id`, `footprint` | KiCad `Library:Name`, when the provider knows them |
| `stock` | units in stock, or `None` when unknown |
| `price` | unit price, or a ladder `[{"qty": 1, "usd": 0.01}, …]` |
| `class` | assembly price class, when the supplier has them (declare the names in `assembly_classes`) |
| `fields` | extra schematic fields to stamp: `{"Shelf": "A1"}` |
| `match` | `"exact"` or `"partial"`: how well it answers the query |

### The interface

| Member | Required | Default behaviour |
|---|---|---|
| `name`, `label`, `part_field` | `name` | — |
| `assembly_classes` | no | `()`; cheapest first, e.g. `("Basic", "Preferred", "Extended")` |
| `generic` | no | `False`; `True` = a Value + Footprint is enough to count as sourced |
| `search(query, filters, limit, live)` | **yes** | filters: `category`, `package`, `in_stock`, `min_stock`, `basic_only` |
| `get(part_id, live)` | **yes** | |
| `owns(part_id)` | no | `False`. Return `True` for ids that are yours so `parts_get(part='SR-0003')` finds you without `provider=`. |
| `pick(spec, live)` | no | the first exact `search` hit; used by `sch_apply` `{"op":"place","spec":"10k 0603"}` |
| `resolve(pr, part, **opts)` | no | symbol + footprint: the record's own `lib_id` / `footprint`, else KiCad stock libraries (chip passives by package, pin headers, a symbol named after the MPN). Override to import drawings or 3D models from your supplier, writing them into the project. |
| `match_component(comp, policy, live)` | no | BOM sourcing: the component's MPN field, then Value + the footprint's package, through `search` |
| `alternatives(target, limit, same_package, live)` | no | other `search` hits for the part's value + package |
| `schematic_fields(rec)` | no | `part_field` ← id, `MPN`, `Manufacturer`, `Datasheet`, `Description`, plus `rec["fields"]` |
| `status()` | no | one line for `copper_status` / `copper doctor` |
| `health()` | no | `None`. Return `{"message": ..., "fix": ...}` when the provider cannot work (missing API key, missing file); `copper_status` and `project_summary` then lead with it. Must not use the network. |
| `source_bom(pr, policy)` | no | implement only to replace the whole sourcing pass when you are the single active provider |

The constructor receives `(options, project_dir)`: `options` is `parts.options.<name>` from the config.
Raise `copper_kicad.mcp.registry.ToolError(message, fix=..., code=...)` for failures the user can act on; any other
exception from a provider is reported as a warning (`provider_errors` in `parts_search`, `warnings` in `sch_apply`,
`notes` on a BOM line) and the next provider in the list is asked; a call that named the provider (`provider=…`)
fails with `PROVIDER_FAILED` and the provider's name. Records without an `id` are dropped and counted.

A class that does not subclass `Provider` works too, as long as it has `name`, `search` and `get`.

### Making Copper find it

**A path in the config** (no packaging):

```json
{"parts": {"providers": ["myshop", "kicad"], "provider_paths": ["C:/work/myshop_provider.py"]}}
```

A path entry is a `.py` file or an importable module name (`"mycompany.copper_parts"`). The module may define
`PROVIDER = MyProvider`, `PROVIDERS = [...]`, or just contain provider classes. Paths are honoured in the user
config (`$COPPER_HOME/config.json`); a project file cannot load code unless the user file allows it
([CONFIG.md](CONFIG.md), *Safety*).

**A Python entry point** (an installed package), group `copper_kicad.parts`:

```toml
# pyproject.toml of your package
[project.entry-points."copper_kicad.parts"]
myshop = "mycompany.copper_parts:MyProvider"
```

Install the package into the Python environment that runs Copper; the provider is then available by name.

A provider that fails to import or does not meet the interface gives `PROVIDER_LOAD_FAILED` with the file, the line
and a fix. It never takes the server down.

## What Copper does not ship

There are no bundled Digi-Key, Mouser, Farnell or Octopart API clients. Each needs an account, API keys and a
test environment Copper's authors do not have, and an untested client would be fake coverage. The `local`
catalogue covers those distributors today (export your parts list, keep the distributor number in a column), and
the interface above is the supported way to add a live client.
