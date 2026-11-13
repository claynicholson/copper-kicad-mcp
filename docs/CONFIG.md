# Copper configuration

Copper has one settings loader (`src/copper_kicad/config.py`). Everything that could be an assumption about a
vendor is a key in it. With no config file at all Copper uses LCSC parts and the JLCPCB fab profile.

## Layers

Later layers win. Objects merge key by key; lists and plain values replace.

| # | Layer | Where | Scope |
|---|---|---|---|
| 1 | built-in defaults | `config.DEFAULTS` | everyone |
| 2 | user file | `$COPPER_HOME/config.json` (default `~/.copper/config.json`) | every project on this machine |
| 3 | project file | `<project>/.copper/config.json` | one design; travels with the project folder |
| 4 | call arguments | `provider=` on the `parts_*` tools, `profile=` on `fab_export`, `fab=` on `project_create` | one call |

`copper_status` and `project_summary` show the result under `config`: the active parts providers, the fab profile
and the files that set them. `copper doctor` has a *Copper config* line.

## Keys

```jsonc
{
  "parts": {
    "providers": ["lcsc"],            // ordered list; the first provider that answers wins
    "provider_paths": [],             // extra provider modules: a .py file or an importable module name
    "allow_project_providers": false, // USER file only, see "Safety" below
    "catalogue": null,                // file or list of files for the "local" provider (CSV or JSON)
    "options": {},                    // per-provider options: {"<provider name>": {...}}
    "policy": {
      "require_in_stock": true,       // skip parts a provider reports as out of stock (parts_search, BOM sourcing)
      "basic_only": false,            // BOM sourcing: only a provider's cheapest assembly class (JLC Basic)
      "allow_extended": true,         // BOM sourcing: allow a provider's dearest assembly class (JLC Extended)
      "min_stock": 500,               // below this a part is a stock risk
      "part_field": null              // schematic field holding "the" supplier part number;
    }                                 //   null = the first active provider's own field (LCSC, MPN, …)
  },
  "fab": {
    "profile": "jlcpcb"               // jlcpcb | pcbway | oshpark | generic | a profile you added
  }
}
```

Keys starting with `_`, `$` or `//` are ignored, so a file can carry comments.
Relative paths (`catalogue`, `provider_paths`) are relative to the project folder in a project file and to
`$COPPER_HOME` in the user file.

### Examples

A shop that buys from its own stockroom list and sends Gerbers to a local fab:

```json
{
  "parts": {"providers": ["local", "kicad"], "catalogue": "stockroom.csv"},
  "fab": {"profile": "generic"}
}
```

Keep LCSC as a fallback behind the stockroom, order from PCBWay:

```json
{
  "parts": {"providers": ["local", "lcsc"], "policy": {"part_field": "MPN"}},
  "fab": {"profile": "pcbway"}
}
```

One project that goes to OSH Park while everything else stays on the defaults: put this in
`<project>/.copper/config.json` (or create the project with `project_create(name=…, fab='oshpark')`, which writes it):

```json
{"fab": {"profile": "oshpark"}}
```

## Errors

A broken config never crashes a tool. It raises `BAD_CONFIG` with the file, the line (and column for a JSON syntax
error) and a fix, for example:

```
bad Copper config C:\Users\me\.copper\config.json, line 3: 'parts.providers' must be a non-empty list of provider
names, e.g. ["local", "kicad"] (got "local")
fix: set "providers" to a non-empty list of provider names, e.g. ["local", "kicad"]
```

Unknown keys are rejected (a typo such as `"fabs"` would otherwise be silently ignored); the message lists the valid
keys.

A file can also parse and still not work: a provider name nothing provides, a provider module that does not load, a
fab profile that does not exist, a catalogue file that is missing or unreadable. Those are listed under
`config.problems` in `copper_status` and `project_summary`, both tools put "fix the Copper config first: …" in `next`,
`copper doctor` warns, and the tool that needs the missing thing fails with `UNKNOWN_PROVIDER`,
`UNKNOWN_FAB_PROFILE`, `PROVIDER_LOAD_FAILED` or `BAD_CATALOGUE` and the names that do exist. Until it is fixed
`project_summary` reports no sourcing percentage, because which field counts as the part number is not known.

## Safety

A parts provider is Python code. A **project** file may therefore not load one: `parts.provider_paths` in
`<project>/.copper/config.json` is ignored, with a warning in `copper_status`, unless the **user** file says
`"allow_project_providers": true`. Opening somebody else's project never runs their code. Fab profiles are plain
JSON data and are safe to ship inside a project (`<project>/.copper/fabs/`).

## What follows the config

| Setting | Used by |
|---|---|
| `parts.providers` | `parts_search`, `parts_get`, `parts_import`, `parts_source_bom`, `parts_alternatives`, `sch_apply` place by `spec` / `part`, `design_review` sourcing, `copper doctor` network probes |
| `parts.policy.part_field` | which field counts as the part number in `project_summary`, `design_review`, and in BOMs of profiles that do not fix one |
| `fab.profile` | `fab_export`, `project_create` (rules of a new project), `pcb_setup(rules='fab')` and its first-time default, `verify` hint text, `export(kind='gerbers' / 'drill' / 'bom' / 'ipc2581')` |
