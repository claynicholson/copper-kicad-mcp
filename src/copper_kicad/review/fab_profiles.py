"""Fab profiles: everything Copper knows about a PCB manufacturer, as data.

A profile is one JSON file. Lookup order for a name (first hit wins):

    <project>/.copper/fabs/<name>.json   >   $COPPER_HOME/fabs/<name>.json   >   built-in data/fabs/<name>.json

Built in: ``jlcpcb`` (default), ``pcbway``, ``oshpark`` and ``generic``. A profile may start from another
with ``"extends": "<name>"`` and override only what differs. Which profile is active comes from
``config.fab.profile`` (see config.py) or the per-call ``profile=`` argument.

Keys (all optional except ``name``; DEFAULTS fills the rest)::

    name, label, short, url, notes            notes says which numbers were checked against the fab and which were not
    rules.{standard,advanced}                 design-rule minimums in mm (the pcb_setup / project_create rule presets)
    rules_by_layers.{"4": {...}}              overrides of rules.standard for that copper-layer count
    presets {"jlc": "standard"}               extra pcb_setup(rules=…) names this profile answers to
    templates {"2": "jlc-2layer"}             project template whose files already hold this profile's rules
    gerber {x2, netlist_attributes, protel_extensions, subtract_soldermask, precision, job_file, layers[]}
    drill {units, zeros, oval, origin, separate_th}
    naming {gerber_dir, zip, bom, placement}  file names; {name} = project name
    zip {folder, include_assembly}            folder inside the zip; also put BOM + placement in it
    assembly {offered}                        false = bare boards only (BOM/placement only when asked)
    bom {part_field, part_label, columns[[header, template]]}
                                              templates: {value} {refs} {qty} {footprint} {part} {mpn} {manufacturer}
                                              {description} {item} {mount} {supplier} {supplier_notes} {field:<Any Field>}
    placement {format: "columns"|"kicad", columns, position: "pads_center"|"anchor", layer_names, rotation{…}}
                                              rotation.table = a corrections file (data/ or next to the profile)
    part_classes {field, aliases, fee_usd}    assembly price classes, when the fab has them
    blocks [codes]                            what refuses an export: erc drc unrouted parity parts outline empty
    limits {...}, order_notes [...]           advice printed with the pack
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from ..mcp.registry import ToolError

DATA = Path(__file__).resolve().parent.parent / "data"
BUILTIN_DIR = DATA / "fabs"
DEFAULT_PROFILE = "jlcpcb"

BLOCK_CODES = ("erc", "drc", "unrouted", "parity", "parts", "outline", "empty")
RULE_KEYS = ("min_track", "min_clearance", "min_via_diameter", "min_via_drill", "min_annular", "min_hole",
             "hole_to_hole", "edge_clearance", "hole_clearance", "silk_clearance", "min_text_height",
             "min_text_thickness")
GERBER_LAYERS = ["copper", "F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]

DEFAULTS: dict = {
    "label": "", "short": "", "url": "", "notes": "",
    "rules": {"standard": {
        "label": "", "min_track": 0.15, "min_clearance": 0.15, "min_via_diameter": 0.6, "min_via_drill": 0.3,
        "min_annular": 0.15, "min_hole": 0.3, "hole_to_hole": 0.5, "edge_clearance": 0.3, "hole_clearance": 0.25,
        "silk_clearance": 0.0, "min_text_height": 1.0, "min_text_thickness": 0.15, "via": [0.6, 0.3]}},
    "rules_by_layers": {},
    "presets": {},
    "templates": {},
    "gerber": {"x2": True, "netlist_attributes": True, "protel_extensions": False, "subtract_soldermask": False,
               "precision": 6, "job_file": False, "layers": GERBER_LAYERS},
    "drill": {"units": "mm", "zeros": "decimal", "oval": "alternate", "origin": "absolute", "separate_th": True},
    "naming": {"gerber_dir": "gerbers", "zip": "{name}-fab.zip", "bom": "{name}-BOM.csv", "placement": "{name}-pos.csv"},
    "zip": {"folder": "", "include_assembly": False},
    "assembly": {"offered": True},
    "bom": {"part_field": None, "part_label": "part number",
            "columns": [["Refs", "{refs}"], ["Qty", "{qty}"], ["Value", "{value}"], ["Footprint", "{footprint}"],
                        ["MPN", "{mpn}"], ["Manufacturer", "{manufacturer}"], ["Supplier Part #", "{supplier}"]]},
    "placement": {"format": "kicad", "side": "both", "units": "mm", "position": "pads_center",
                  "layer_names": {"top": "top", "bottom": "bottom"},
                  "columns": [["Ref", "{ref}"], ["Val", "{value}"], ["Package", "{footprint}"], ["PosX", "{x:.4f}"],
                              ["PosY", "{y:.4f}"], ["Rot", "{rotation:g}"], ["Side", "{layer}"]],
                  "rotation": {}},
    "part_classes": {},
    "blocks": ["erc", "drc", "unrouted", "parity", "outline", "empty"],
    "limits": {},
    "order_notes": [],
}


def _dirs(pr=None) -> list[tuple[str, Path]]:
    from .. import config
    out = []
    if pr is not None:
        out.append(("project", Path(pr.dir) / ".copper" / "fabs"))
    out.append(("user", config.home() / "fabs"))
    out.append(("built-in", BUILTIN_DIR))
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_.\-]", "", str(name or "").strip().lower())


def available(pr=None) -> list[dict]:
    """Every profile a project can use: [{'name','label','scope','path'}], nearest scope first."""
    seen, out = set(), []
    for scope, d in _dirs(pr):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            n = f.stem.lower()
            if n in seen:
                continue
            seen.add(n)
            label = n
            try:
                label = json.loads(f.read_text(encoding="utf-8-sig")).get("label") or n
            except (OSError, ValueError, AttributeError):
                label = f"{n} (unreadable)"
            out.append({"name": n, "label": label, "scope": scope, "path": str(f)})
    return out


def names(pr=None) -> list[str]:
    return [p["name"] for p in available(pr)]


def find(name: str, pr=None) -> Path | None:
    n = _slug(name)
    for _scope, d in _dirs(pr):
        f = d / f"{n}.json"
        if f.is_file():
            return f
    return None


def _bad(path: Path, message: str, fix: str, line: int | None = None, col: int | None = None) -> ToolError:
    where = str(path) + (f", line {line}" if line else "") + (f", column {col}" if col else "")
    return ToolError(f"bad fab profile {where}: {message}", code="BAD_FAB_PROFILE", fix=fix,
                     data={"file": str(path), **({"line": line} if line else {})})


def _line_of(text: str, key: str) -> int | None:
    m = re.search(r'"' + re.escape(key) + r'"\s*:', text)
    return text.count("\n", 0, m.start()) + 1 if m else None


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def _read(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise _bad(path, f"cannot be read ({e})", "make it readable UTF-8 JSON")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise _bad(path, e.msg, "fix the JSON syntax there (copy a built-in profile from fab_export(profile='list') "
                                "as a starting point)", e.lineno, e.colno)
    if not isinstance(data, dict):
        raise _bad(path, "the top level must be a JSON object", 'start with {"name": "...", "extends": "generic"}', 1)
    return data, text


def load(name: str, pr=None, _chain: tuple = ()) -> dict:
    """A complete, validated profile (defaults and ``extends`` resolved)."""
    n = _slug(name)
    path = find(n, pr)
    if path is None:
        have = names(pr)
        raise ToolError(f"unknown fab profile '{name}'", code="UNKNOWN_FAB_PROFILE",
                        fix=f"use one of {have}, or add {n or '<name>'}.json to $COPPER_HOME/fabs/ or "
                            f"<project>/.copper/fabs/ (docs/FAB_PROFILES.md)", data={"profiles": have})
    raw, text = _read(path)
    base = copy.deepcopy(DEFAULTS)
    parent = raw.get("extends")
    if parent:
        if not isinstance(parent, str) or _slug(parent) in _chain + (n,):
            raise _bad(path, f"'extends' must name another profile (got {parent!r})",
                       'e.g. "extends": "generic"', _line_of(text, "extends"))
        if find(parent, pr) is None:
            raise _bad(path, f"'extends' names an unknown profile '{parent}'", f"use one of {names(pr)}",
                       _line_of(text, "extends"))
        base = load(parent, pr, _chain + (n,))
        for k in ("_source", "_scope", "extends", "presets", "templates"):
            base.pop(k, None)
        base.setdefault("presets", {})
        base.setdefault("templates", {})
    prof = _merge(base, {k: v for k, v in raw.items() if not k.startswith(("_", "$", "//"))})
    prof["name"] = n
    prof["label"] = prof.get("label") or n
    prof["short"] = prof.get("short") or prof["label"]
    prof["_source"] = str(path)
    prof["_scope"] = next((s for s, d in _dirs(pr) if d == path.parent), "built-in")
    _validate(prof, path, text)
    return prof


def _validate(p: dict, path: Path, text: str) -> None:
    def need(cond, key, msg, fix):
        if not cond:
            raise _bad(path, f"'{key}' {msg}", fix, _line_of(text, key.rsplit(".", 1)[-1]))

    for sect in ("rules", "gerber", "drill", "naming", "zip", "assembly", "bom", "placement", "limits",
                 "rules_by_layers", "presets", "templates", "part_classes"):
        need(isinstance(p.get(sect), dict), sect, "must be an object", f'"{sect}": {{...}}')
    need(isinstance(p["rules"].get("standard"), dict), "rules.standard", "is required",
         '"rules": {"standard": {"min_track": 0.15, "min_clearance": 0.15, ...}}')
    tiers = list(p["rules"].items()) + [(f"rules_by_layers.{k}", v) for k, v in p["rules_by_layers"].items()]
    for tier, r in tiers:
        need(isinstance(r, dict), tier, "must be an object", "an object of design-rule minimums in mm")
        for k in RULE_KEYS:
            if k in r:
                need(isinstance(r[k], (int, float)) and not isinstance(r[k], bool) and 0 <= r[k] <= 50, k,
                     f"in {tier} must be a size in mm (got {r[k]!r})", f'"{k}": 0.15')
        for k in ("via", "power_via"):
            if k in r:
                need(isinstance(r[k], list) and len(r[k]) == 2 and all(isinstance(x, (int, float)) for x in r[k])
                     and r[k][0] > r[k][1] > 0, k, f"in {tier} must be [diameter_mm, drill_mm]", f'"{k}": [0.6, 0.3]')
    need(isinstance(p.get("blocks"), list) and all(b in BLOCK_CODES for b in p["blocks"]), "blocks",
         f"must be a list drawn from {list(BLOCK_CODES)}", '"blocks": ["erc", "drc", "unrouted", "parity", "outline", "empty"]')
    for sect, key in (("bom", "columns"), ("placement", "columns")):
        cols = p[sect].get(key)
        need(isinstance(cols, list) and cols and all(isinstance(c, list) and len(c) == 2 and all(isinstance(x, str) for x in c)
                                                     for c in cols), "columns",
             f"in {sect} must be a list of [header, template] pairs", '"columns": [["Refs", "{refs}"], ["Value", "{value}"]]')
    need(p["placement"].get("format") in ("columns", "kicad"), "format", "in placement must be 'columns' or 'kicad'",
         '"format": "kicad" (KiCad\'s own position file) or "columns" (your own columns)')
    need(p["placement"].get("position", "pads_center") in ("pads_center", "anchor"), "position",
         "in placement must be 'pads_center' or 'anchor'", '"position": "pads_center"')
    layers = p["gerber"].get("layers")
    need(isinstance(layers, list) and layers and all(isinstance(x, str) for x in layers), "layers",
         "in gerber must be a list of layer names ('copper' = every copper layer)", f'"layers": {json.dumps(GERBER_LAYERS)}')
    for k in ("zip", "bom", "placement", "gerber_dir"):
        need(isinstance(p["naming"].get(k), str) and p["naming"][k].strip() and not re.search(r"[\\/:*?\"<>|]", p["naming"][k]),
             k, "in naming must be a plain file name ({name} = project name)", f'"{k}": "{{name}}-fab.zip"')
    pf = p["bom"].get("part_field")
    need(pf is None or (isinstance(pf, str) and pf.strip()), "part_field", "in bom must be a field name or null",
         '"part_field": "MPN"')
    rot = p["placement"].get("rotation") or {}
    need(isinstance(rot, dict), "rotation", "in placement must be an object", '"rotation": {"table": "my_rotations.json"}')
    for k in ("native_libraries", "native_source"):
        if rot.get(k):
            try:
                re.compile(rot[k])
            except re.error as e:
                need(False, k, f"is not a valid regular expression ({e})", "fix the pattern or remove the key")
    if rot.get("table") and rotation_table_path(p) is None:
        need(False, "table", f"names a rotation table that does not exist ({rot['table']})",
             "put the file next to the profile, or remove the key")


def rotation_table_path(profile: dict) -> Path | None:
    t = ((profile.get("placement") or {}).get("rotation") or {}).get("table")
    if not t:
        return None
    for base in (Path(profile.get("_source", "")).parent, DATA, BUILTIN_DIR):
        f = base / t
        if f.is_file():
            return f
    return None


def active_name(pr=None, name: str | None = None) -> str:
    if name:
        return _slug(name)
    from .. import config
    return config.load(pr).fab_profile


def active(pr=None, name: str | None = None) -> dict:
    return load(active_name(pr, name), pr)


def rules(profile: dict, tier: str = "standard", layers: int | None = None) -> dict:
    """One rule set in the shape pcb/rules.py uses (``via`` as a tuple, every key present)."""
    r = copy.deepcopy(DEFAULTS["rules"]["standard"])
    r.update(profile["rules"]["standard"])
    if tier and tier != "standard":
        if tier not in profile["rules"]:
            raise ToolError(f"fab profile '{profile['name']}' has no '{tier}' rule set", code="BAD_ARGUMENT",
                            fix=f"use one of {sorted(profile['rules'])}")
        r.update(profile["rules"][tier])
    elif layers and str(layers) in profile.get("rules_by_layers", {}):
        r.update(profile["rules_by_layers"][str(layers)])
    r["label"] = r.get("label") or f"{profile['label']} {tier}"
    r["via"] = tuple(r["via"])
    r["min_via_diameter"] = max(r["min_via_diameter"], 0)
    if "power_via" in r:
        r["power_via"] = tuple(r["power_via"])
    r["profile"] = profile["name"]
    return r


def preset(name: str, pr=None, layers: int | None = None) -> dict | None:
    """Resolve a pcb_setup(rules=…) name: 'fab' (the active profile), '<profile>', '<profile>_advanced' /
    '<profile>:advanced', or an alias a profile declares in ``presets`` (jlc, jlc_advanced)."""
    n = str(name or "").strip().lower()
    if n in ("fab", "default"):
        return rules(active(pr), "standard", layers)
    m = re.fullmatch(r"([a-z0-9][a-z0-9_.\-]*?)(?:[:_](standard|advanced))?", n)
    if m and find(m.group(1), pr):
        return rules(load(m.group(1), pr), m.group(2) or "standard", layers)
    if find(n, pr):
        return rules(load(n, pr), "standard", layers)
    for p in available(pr):
        try:
            prof = load(p["name"], pr)
        except ToolError:
            continue
        if n in prof.get("presets", {}):
            return rules(prof, prof["presets"][n], layers)
    return None


def preset_names(pr=None) -> list[str]:
    out = ["fab"]
    for p in available(pr):
        try:
            prof = load(p["name"], pr)
        except ToolError:
            continue
        out.append(p["name"])
        out += [f"{p['name']}_{t}" for t in prof["rules"] if t != "standard"]
        out += list(prof.get("presets", {}))
    return list(dict.fromkeys(out))


def describe(profile: dict) -> dict:
    """The short public view (fab_export(profile='list'), copper_status)."""
    r = profile["rules"]["standard"]
    return {"name": profile["name"], "label": profile["label"], "scope": profile.get("_scope"),
            "file": profile.get("_source"), "assembly": bool(profile["assembly"].get("offered")),
            "bom_part_field": profile["bom"].get("part_field") or "(project policy)",
            "rotation_table": bool((profile["placement"].get("rotation") or {}).get("table")),
            "blocks": list(profile["blocks"]),
            "min_track_mm": r.get("min_track"), "min_clearance_mm": r.get("min_clearance"),
            "min_via_mm": [r.get("min_via_diameter"), r.get("min_via_drill")], "notes": profile.get("notes", "")}
