# Fab profiles

A fab profile is one JSON file holding everything Copper knows about a PCB manufacturer: design-rule minimums,
how Gerbers and drill files are plotted and named, the zip layout, BOM and placement columns, assembly rotation
corrections, which part-number field the BOM uses, and what blocks an order. `fab_export` writes the order pack
for a profile; `project_create` and `pcb_setup` take their design rules from it.

The active profile is `fab.profile` in the Copper config ([CONFIG.md](CONFIG.md)), `jlcpcb` unless changed.
`fab_export(profile='list')` shows every available profile with its key numbers and notes.

## Built-in profiles

| Profile | Gerbers | Drill | BOM | Placement | Blocks on missing part numbers |
|---|---|---|---|---|---|
| `jlcpcb` (default) | Protel extensions, no X2, soldermask subtracted from silkscreen | Excellon mm, PTH + NPTH files | `Comment, Designator, Footprint, LCSC Part #` | `Designator, Mid X, Mid Y, Layer, Rotation`, pad-centre position, JLC rotation table | yes (`LCSC`) |
| `pcbway` | Protel extensions, no X2 | Excellon mm, PTH + NPTH | `Item #, Designator, Qty, Manufacturer, Mfg Part #, Description / Value, Package/Footprint, Type, Notes` | KiCad position file (CSV, mm) | no (listed in the result) |
| `oshpark` | Protel extensions, X2 | Excellon mm, one merged file | bare boards: BOM/placement only with `assembly=true` | KiCad position file | no |
| `generic` | `.gbr`, Gerber X2, with the `.gbrjob` job file | Excellon mm, PTH + NPTH | `Refs, Qty, Value, Footprint, MPN, Manufacturer, Supplier Part #` | KiCad position file (CSV, mm, both sides, DNP excluded) | no |

### How the numbers were checked

Each profile's `notes` field says, in words, which values were checked against the fab's published capabilities
and which were not. Summary of the check made on 2026-11-09:

| Profile | Confirmed against the fab's own pages | Not confirmed (marked UNVERIFIED in `notes`) |
|---|---|---|
| `jlcpcb` | Published minimums (track/space 0.10 mm 1-2 layer, 0.09 mm multilayer; via hole 0.15 mm, via diameter 0.25 mm; copper to routed edge 0.2 mm; silkscreen 1.0 mm / 0.15 mm; max drill 6.3 mm; NPTH 0.5 mm; pad-hole spacing 0.45 mm). Every `standard` value is at or above them, except `min_text_height`. | the 0.3 mm no-surcharge via-drill threshold, the 100×100 mm price tier, the 10×10 mm assembly minimum, the $3 Extended fee, `min_text_height` 0.8 mm (JLCPCB publishes 1.0 mm), the JLC04161H-7628 stackup name |
| `pcbway` | track/space 0.1 mm; drill 0.15–6.0 mm; annular ring 0.15 mm; silkscreen 0.8 mm / 0.15 mm; hole-to-hole 16 mil; track to milled edge 0.25 mm; Protel extensions with X2 off; a KiCad `.pos` file is accepted as the centroid file | that 6 mil and 0.3 mm vias are the no-surcharge tier; the `advanced` via size; `hole_clearance`; `npth_min`; the BOM column headers; merged vs separate drill files |
| `oshpark` | 2-layer: 6 mil track/space, 10 mil drill, 5 mil annular ring, 15 mil edge keepout. 4-layer: 5 mil, 10 mil drill, 4 mil ring. X2 recommended; drill origin absolute, decimal zeros, alternate oval mode | silkscreen minimums; `hole_to_hole`; `hole_clearance`; `npth_min`; that PTH and NPTH should be merged |
| `generic` | nothing: it is a conservative common denominator, not a fab | all design rules, by definition |

Capabilities change. Before a production order, compare the profile with the fab's current page, and fix the
profile (your own copy) rather than working around it.

## Adding a profile

Put `<name>.json` in `$COPPER_HOME/fabs/` (all projects) or `<project>/.copper/fabs/` (one project). A nearer
file with the same name replaces a built-in one. Start from another profile with `"extends"` and override only
what differs:

```json
{
  "name": "acme",
  "label": "ACME Circuits",
  "extends": "generic",
  "notes": "Numbers from acme-circuits.example/capabilities, checked 2026-11-09.",
  "rules": {"standard": {"label": "ACME 8 mil", "min_track": 0.2, "min_clearance": 0.2, "edge_clearance": 0.4}},
  "gerber": {"x2": false, "protel_extensions": true, "job_file": false},
  "naming": {"zip": "ACME_{name}.zip", "bom": "ACME_{name}_bom.csv", "placement": "ACME_{name}_xy.csv"},
  "zip": {"folder": "{name}", "include_assembly": true},
  "bom": {"part_field": "Stock No", "part_label": "stockroom number",
          "columns": [["Line", "{item}"], ["Qty", "{qty}"], ["Stock No", "{part}"], ["MPN", "{mpn}"],
                      ["Refs", "{refs}"], ["Shelf", "{field:Shelf}"]]},
  "placement": {"format": "columns", "position": "anchor",
                "columns": [["RefDes", "{ref}"], ["X_mm", "{x:.3f}"], ["Y_mm", "{y:.3f}"],
                            ["Angle", "{rotation:.1f}"], ["Side", "{layer}"]],
                "layer_names": {"top": "T", "bottom": "B"}},
  "blocks": ["erc", "drc", "unrouted", "parity", "parts", "outline", "empty"]
}
```

Then `fab_export(profile='acme')`, or set `"fab": {"profile": "acme"}` in the config. A profile with a mistake
gives `BAD_FAB_PROFILE` with the file, the line and a fix; `tests/test_neutral.py` exercises a profile like the one
above.

## Reference

Every key is optional except `name`; anything left out comes from `extends`, else from the defaults in
`review/fab_profiles.py`.

| Key | Meaning |
|---|---|
| `name`, `label`, `short`, `url` | identity; `short` is used in messages ("JLC") |
| `extends` | name of the profile to start from |
| `notes` | free text. Say where each number came from and which are unverified. Shown by `fab_export(profile='list')`. |
| `rules.standard` | design-rule minimums in mm: `min_track`, `min_clearance`, `min_via_diameter`, `min_via_drill`, `min_annular`, `min_hole`, `hole_to_hole`, `edge_clearance`, `hole_clearance`, `silk_clearance`, `min_text_height`, `min_text_thickness`, `via` `[diameter, drill]` (default via), `power_via`, `pth_annular`, `max_hole`, `npth_min`, `label` |
| `rules.advanced` | a tighter tier, selected with `pcb_setup(rules='<profile>_advanced')` |
| `rules_by_layers` | `{"4": {...}}`: overrides of `rules.standard` for that copper-layer count |
| `presets` | `{"jlc": "standard"}`: extra `pcb_setup(rules=…)` names the profile answers to |
| `templates` | `{"2": "jlc-2layer"}`: a project template whose files already hold this profile's rules |
| `gerber` | `x2`, `netlist_attributes`, `protel_extensions`, `subtract_soldermask`, `precision` (5 or 6), `job_file` (put the `.gbrjob` in the zip), `layers` (`"copper"` = every copper layer) |
| `drill` | `units` (mm / in), `zeros` (decimal / suppressleading / suppresstrailing / keep), `oval` (alternate / route), `origin` (absolute / plot), `separate_th` |
| `naming` | `gerber_dir`, `zip`, `bom`, `placement`; `{name}` = project name, `{profile}` = profile name |
| `zip` | `folder` (a folder inside the zip), `include_assembly` (also zip the BOM and placement file) |
| `assembly.offered` | `false` for bare-board fabs: BOM and placement are written only with `assembly=true` |
| `bom.part_field` | the schematic field that is "the part number" for this fab; `null` = the project's (`parts.policy.part_field`, else the first provider's) |
| `bom.part_label` | how messages name it ("LCSC number") |
| `bom.columns` | `[[header, template], …]`. Templates: `{value}` `{refs}` `{qty}` `{footprint}` `{part}` `{mpn}` `{manufacturer}` `{description}` `{item}` (line number) `{mount}` (SMD / THT) `{supplier}` `{supplier_notes}` `{field:<Any Schematic Field>}` |
| `placement.format` | `"kicad"`: KiCad's own position file, untouched. `"columns"`: Copper writes the rows. |
| `placement.columns` | for `"columns"`: `{ref}` `{x}` `{y}` `{rotation}` `{layer}` `{value}` `{footprint}`, with format specs (`{x:.4f}`) |
| `placement.position` | `pads_center` (centre of the soldered pads, what most pick-and-place services expect) or `anchor` |
| `placement.layer_names` | `{"top": "Top", "bottom": "Bottom"}` |
| `placement.rotation` | `table` (a corrections file next to the profile or in Copper's data folder; same format as `data/jlc_rotations.json`), `project_tables` (file names looked up in `<project>/.copper/`, default `rotations.json`), `offset_fields` / `position_offset_fields` (per-part override fields), `native_libraries` / `native_source` (regexes for footprints that already use the fab's orientation and must not be corrected) |
| `part_classes` | `field`, `aliases`, `fee_usd`: assembly price classes, for fabs that have them |
| `blocks` | which findings refuse the export: `erc`, `drc`, `unrouted`, `parity`, `parts` (an assembled part without `bom.part_field`), `outline`, `empty`. Findings not listed are reported under `advisories` instead. |
| `limits` | advice thresholds: `price_tier_mm`, `assembly_min_mm`, `small_drill_surcharge_mm`, `through_vias_only`, `tht_note`, `stackup_notes` |
| `order_notes` | lines appended to the pack's order notes |

## Projects and design rules

* `project_create(name=…, fab='pcbway')` creates the project with that profile's rules in the `.kicad_pro` and
  a managed block in the `.kicad_dru`, and records the profile in `<project>/.copper/config.json`.
* The template names `jlc-2layer` and `jlc-4layer` keep working and mean the `jlcpcb` profile; `2layer` / `4layer`
  mean "this layer count, the configured profile".
* `pcb_setup(rules='fab')` re-applies the active profile's rules to an existing board; `rules='<profile>'` or
  `'<profile>_advanced'` picks one explicitly. The first setup of a board uses the active profile.
