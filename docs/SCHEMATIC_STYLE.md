# Schematic style

How Copper draws, and reviews, a schematic. The aim is a sheet a stranger understands in the blink of an eye.

These rules come from the KiCon talk on drawing good schematics (<https://www.youtube.com/watch?v=X0hd_v8qRiY>),
including the suggestions from its Q&A. **Where they disagree with an older Copper convention, these rules win.**
The source of truth is `RULES` in `src/copper_kicad/review/sch_style.py`; a test keeps this page in step with it.

Rules marked *checked* are measured by `design_review(checks=['schematic_style'])` (also part of
`verify(what=['review'])`) from the saved sheet files; the finding type is given. The others need judgement and
live in the `kicad-design` and `design-review` skills.

## Readability

- **S0 Blink test.** A reader should recognise every circuit on a sheet in about 100 ms. Standardise, simplify, modularise and communicate function. Draw the classic design patterns (divider, RC filter, follower, inverter, LDO, crystal) in their textbook shape so they are recognised, not decoded. Apply every rule with judgement: follow it until it plainly makes the sheet worse, then break it and say why in a note.

## Flow

- **F1 Left-to-right flow.** Signal and power flow left to right: inputs on the left, outputs on the right. When that cannot hold, give the path enough whitespace that the eye can follow it. Enables, soft-start and other control inputs sit with the inputs; a switcher's feedback pin on the right is fine (it belongs to the output circuit).
- **F2 Supplies up, grounds down.** Ground and negative-supply symbols always point down; positive-supply symbols always point up. Never sideways, never upside down. *Checked: `power_symbol_direction`.*
- **F3 Show direction.** Where direction is not obvious (bidirectional or RF chains, TX and RX paths) draw arrows or note which way the signal flows.

## Symbols

- **Y1 Functional symbols.** Symbols are drawn by function, not as the package pinout: supplies on top, grounds at the bottom, inputs left, outputs right, related pins (crystal, bus, port) together. The layout shows the package; cross-probe from the schematic instead of pasting a pinout image. Only RF parts whose schematic is the layout are exempt. *Checked: `package_shaped_symbol`.*
- **Y2 IEEE 315 symbols.** Use IEEE Std 315 symbol conventions. Every mark has a meaning: the envelope circle on a transistor marks a discrete package and must be drawn on discrete transistors. *Checked: `transistor_without_envelope`.*
- **Y3 Hidden duplicate power pins.** Hide (or stack) duplicate supply and ground pins so a symbol shows one of each, unless the design is safety-critical, where every pin is shown.
- **Y4 Filled symbol bodies.** Symbol bodies have a background fill, not a transparent body. *Checked: `symbol_body_unfilled`.*

## Grouping and sheets

- **G1 Whitespace groups.** Whitespace is the strongest separator. Group the parts of one function and separate groups with space. Boxes are visual noise: use a frame only when the sheet is tight.
- **G2 Titled blocks.** Give every functional group a title ("USB-C input", "3V3 LDO").
- **G3 Minimal but not crammed sheets.** Use as few sheets as you can without cramming: one sheet per functional module (power supply, MCU, connectors), no paging through eight sheets for nothing. Prefer 11x17 in (tabloid / A3-class) sheets; the schematic is read on screen. When a sheet runs out of whitespace, add a sheet. *Checked: `sheet_crowded`.*
- **G4 Block diagram on the root sheet.** On a multi-sheet design the root sheet is a block diagram made of hierarchical sheet symbols, wired in signal-flow order, with no circuitry of its own. *Checked: `root_not_block_diagram`.*

## Connections

- **C1 Wires over labels.** Connect with wires whenever you can: a wire is seen in 100 ms, a label must be searched for. Labels are for off-sheet connections and for runs that would be truly awkward to draw. *Checked: `labels_instead_of_wires`.*
- **C2 No label hops between neighbours.** Never connect two nearby symbols with a pair of labels. Draw the wire. *Checked: `label_between_neighbours`.*
- **C3 Name wires too.** Put a net label on important wired nets as well, so a reader can match names and double-check.
- **C4 Global and hierarchical labels.** To the reader global and hierarchical labels are equal: use hierarchical labels for sheets on the block diagram, global labels for nets that truly go everywhere.
- **C5 Inter-sheet references.** Turn on inter-sheet references (Schematic Setup > General > Formatting) so every global label says which sheet(s) its net goes to. *Checked: `intersheet_refs_off`.*
- **C6 At most three wires at a junction.** A junction joins at most three wires. Never draw a four-way junction: stagger it into two tees. *Checked: `four_way_junction`.*
- **C7 Sense wires at 45 degrees.** A sense (Kelvin) wire leaves the conductor it senses at 45 degrees at the exact sense point.

## Power

- **P1 Descriptive rail names.** Name rails after their source or use: VUSB, VBAT, +3V3_MCU, not VCC or VDD. *Checked: `generic_rail_name`.*
- **P2 Rail specs on the sheet.** Annotate each rail with min / typ / max voltage and typical / maximum current, e.g. "VUSB 4.5 / 5.0 / 5.5 V, 35 mA typ, 500 mA max", or at least "9-19 V in" and "3V3, 2 A max". *Checked: `rail_without_spec`.*
- **P3 Power through power symbols.** Distribute power with power symbols (or wires), never with ordinary net labels.
- **P4 0 V is not earth.** Circuit common (0 V / GND), chassis and earth are different nets with different symbols. Do not draw the earth symbol for circuit common; name what each reference is. *Checked: `earth_symbol_on_common, zero_volt_and_gnd`.*

## Documentation

- **D1 Notes everywhere.** Use text notes freely: what a test point should read (voltage, waveform, scope setting), why a value was chosen, what the block does. *Checked: `test_point_no_expectation`.*
- **D2 Call out the weird.** Say so in a note when something is intentional but odd: pins left floating on purpose, inputs shorted on purpose, a part fitted only for one variant. *Checked: `floating_input_unexplained`.*
- **D3 Full passive specs.** Resistors, capacitors, inductors and diodes carry value, tolerance, power or voltage rating, and package ("10k 1% 0.1W 0402"). Check the package can dissipate the rating. Not needed for ICs. *Checked: `passive_spec_incomplete`.*
- **D4 Standard-parts note on every sheet.** Every sheet carries a note with the defaults ("Unless noted: resistors 1% 0.063W 0402; capacitors X7R 16V 0402") so parts that follow them need only their value. *Checked: `no_standard_parts_note`.*

## Release checklist

- **K1 Checklist.** Before release: power is distributed through power symbols, every supply points the right way, important nets are descriptively named, MOSFETs are oriented correctly for their body diode, every IC has its bypass capacitors, and the design fails safe, with modules (power supplies, I/O) grouped separately.

## What changed in Copper to follow these rules

| Older convention | Now |
|---|---|
| Every connection a stub and a net label at the pin | Wires first (C1, C2): `sch_apply` batches use `wire` ops between parts of one block, and `sch_tidy` wires in-block nets; labels are for off-sheet and awkward runs |
| `VCC` / `VDD` used as example rail names | Rails are named after their source or use (P1) and carry a min / typ / max note (P2) |
| Passives carry a value only | Value, tolerance, rating, package, or a standard-parts note per sheet (D3, D4) |
| Smallest paper that fits | Still the default of `sch_tidy`, but a crowded sheet is split, not shrunk; prefer `paper='A3'` (the 11x17 class) for anything that is not tiny (G3) |
| Frames around every block | Whitespace separates, titles name (G1, G2). `sch_tidy` keeps its titled frames because the titles and notes hang on them; on a roomy hand-drawn sheet leave the boxes out |
| Inter-sheet references off in new projects | On in every project template (C5) |
| "Power at top, GND at bottom" as a layout habit | A hard rule about symbol direction, including negative rails pointing down (F2) |

## Known gaps

- C7 (sense wires at 45 degrees), Y3 (hidden duplicate power pins) and C3 (names on wired nets) are guidance only: Copper's
  router draws orthogonal wires and never edits KiCad stock symbols.
- `sch_tidy` does not build hierarchical sheets, so G4 (block diagram on the root) is checked, not generated.
- The package-shape check (Y1) only recognises symbols whose pin numbers run round the body in package order.

## References

- Edward Tufte, *The Visual Display of Quantitative Information* (whitespace, visual flow, chartjunk).
- Horowitz and Hill, *The Art of Electronics*, appendix on drawing schematics.
- IEEE Std 315, *Graphic Symbols for Electrical and Electronics Diagrams*.
- Olin Lathrop, "Rules and guidelines for drawing good schematics", Electrical Engineering Stack Exchange.
