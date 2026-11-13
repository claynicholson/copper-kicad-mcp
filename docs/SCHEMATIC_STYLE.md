# Schematic style

Drawing rules for a schematic a stranger understands in the blink of an eye. They come from the KiCon talk on
drawing good schematics (<https://www.youtube.com/watch?v=X0hd_v8qRiY>), including the suggestions from its Q&A.
Apply them with judgement; when one plainly makes a sheet worse, break it and say why in a note.

The list is `RULES` in `src/copper_kicad/review/sch_style.py`; `tests/test_sch_style.py` checks that every rule line
below matches it. Agents read the same list with `copper_help(tool='design_review')`.

`design_review(checks=['schematic_style'])` measures the rules marked *Checked* from the saved sheet files. It runs
only when asked for: it is not part of the default `design_review` or of `verify(what=['review'])`. Its findings come
back under `style`, apart from the engineering findings, each with the `rule` id. None is an error, and no `fix`
changes the netlist: it is either a call that keeps every pin on its net or a plain sentence.

## Readability

- **S0 Blink test.** A reader should recognise every circuit on a sheet in about 100 ms. Standardise, simplify, modularise and communicate function. Draw the classic design patterns (divider, RC filter, follower, inverter, LDO, crystal) in their textbook shape so they are recognised, not decoded. Apply every rule with judgement: follow it until it plainly makes the sheet worse, then break it and say why in a note.

## Flow

- **F1 Left-to-right flow.** Signal and power flow left to right: inputs on the left, outputs on the right. When that cannot hold, give the path enough whitespace that the eye can follow it. Enables, soft-start and other control inputs sit with the inputs; a switcher's feedback pin on the right is fine (it belongs to the output circuit).
- **F2 Supplies up, grounds down.** Ground symbols point down and supply symbols point up, the way the stock power symbols are drawn. Never sideways, never upside down. KiCad's stock negative-rail symbols (-5V, VEE) point up; keep them as drawn. *Checked: `power_symbol_direction`.*
- **F3 Show direction.** Where direction is not obvious (bidirectional or RF chains, TX and RX paths) draw arrows or note which way the signal flows.

## Symbols

- **Y1 Functional symbols.** Symbols are drawn by function, not as the package pinout: supplies on top, grounds at the bottom, inputs left, outputs right, related pins (crystal, bus, port) together. The layout shows the package; cross-probe from the schematic instead of pasting a pinout image. Only RF parts whose schematic is the layout are exempt. *Checked: `package_shaped_symbol`.*
- **Y2 IEEE 315 symbols.** Use IEEE Std 315 symbol conventions. Every mark has a meaning: the envelope circle on a transistor marks a discrete package, so draw it on discrete transistors. *Checked: `transistor_without_envelope`.*
- **Y3 Stacked duplicate power pins.** Stack duplicate supply and ground pins (same position, one visible) so a symbol shows one of each, unless the design is safety-critical, where every pin is shown. Do not hide them: in KiCad a hidden power-input pin joins a global net named after the pin.
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
- **C5 Inter-sheet references.** On a multi-sheet design turn on inter-sheet references (Schematic Setup > General > Formatting) so every global label says which sheet(s) its net goes to. *Checked: `intersheet_refs_off`.*
- **C6 At most three wires at a junction.** A junction joins at most three wires. Never draw a four-way junction: stagger it into two tees. *Checked: `four_way_junction`.*
- **C7 Sense wires at 45 degrees.** A sense (Kelvin) wire leaves the conductor it senses at 45 degrees at the exact sense point.

## Power

- **P1 Descriptive rail names.** Name rails after their voltage or source (+3V3, +5V, VBUS, +BATT), not a bare VCC or VDD. Names from KiCad's power library become power symbols with `connect`; a custom name such as +3V3_MCU needs the `power` op. *Checked: `generic_rail_name`.*
- **P2 Rail specs on the sheet.** Annotate each rail with min / typ / max voltage and typical / maximum current, e.g. "VBUS 4.75 / 5.0 / 5.25 V, 500 mA max", or at least "9-19 V in" and "3V3, 2 A max". *Checked: `rail_without_spec`.*
- **P3 Power through power symbols.** Distribute power with power symbols (or wires), never with ordinary net labels.
- **P4 0 V is not earth.** Circuit common (0 V / GND), chassis and earth are different nets with different symbols. Do not draw the earth symbol for circuit common; name what each reference is. *Checked: `earth_symbol_on_common`, `zero_volt_and_gnd`.*

## Documentation

- **D1 Notes everywhere.** Use text notes freely: what a test point should read (voltage, waveform, scope setting), why a value was chosen, what the block does. *Checked: `test_point_no_expectation`.*
- **D2 Call out the weird.** Say so in a note when something is intentional but odd: pins left floating on purpose, inputs shorted on purpose, a part fitted only for one variant. *Checked: `floating_input_unexplained`.*
- **D3 Full passive specs.** Resistors, capacitors, inductors and diodes carry value, tolerance, power or voltage rating, and package ("10k 1% 0.063W 0402"). Check the package can dissipate the rating. Not needed for ICs. *Checked: `passive_spec_incomplete` (the package only).*
- **D4 Standard-parts note on every sheet.** Every sheet with passives carries a note with the defaults it really uses ("Unless noted: resistors 1% 0.063W 0402; capacitors X7R 16V 0402") so parts that follow them need only their value. *Checked: `no_standard_parts_note`.*

## Release checklist

- **K1 Checklist.** Before release: power is distributed through power symbols, every supply points the right way, important nets are descriptively named, MOSFETs are oriented correctly for their body diode, every IC has its bypass capacitors, and the design fails safe, with modules (power supplies, I/O) grouped separately.

## What the pass does not do

- On a sheet that is still exactly as Copper's layout engine drew it (text notes added later do not count), the
  wiring checks (C1, C2, C6 and sideways power symbols) are skipped. The engine draws the nets inside a block as
  wires and puts a label only where a net leaves its block, by design, and it may keep a power symbol sideways on a
  crowded rail as a last resort. The sheet counts as untouched even if a power symbol was turned by hand later, so
  a sideways one is then not reported either; an upside-down one is.
- On a stub-and-label sheet Copper drew (`sch_apply` with `layout='keep'`, or an older Copper sheet), sideways or
  upside-down power symbols, four-way junctions and label-only nets are notes whose fix is `sch_tidy()`: the layout
  engine applies these rules when it redraws the sheet. Sheets `sch_tidy` cannot re-lay (buses, sheet symbols,
  directive labels, rule areas, shared sheets) are treated as hand-drawn instead.
- On a hand-drawn sheet the F2 `fix` is a sentence: turning a power symbol back to rot 0 is often refused there,
  because its pin has no room. A negative rail drawn pointing down is a common convention, so it is only a note.
- C2 and C1 only count labels that no wire joins. The C2 `fix` wires two pins of two different parts that are
  already on the labelled net, found through the wiring, by pin number; when no such pins are found it is a sentence.
- D4 is one finding for the whole design, listing the sheets that have passives and no note.
- D3 only asks for a package: a part with a footprint, or a package named in its value or a field, passes.
  Tolerance and rating are left to the standard-parts note (D4): which parts differ from the note cannot be read
  from the drawing.
- Y1, Y4, G3, G4 and C5 have no Copper op to fix them; their `fix` says what to do in KiCad. Y1 and Y4 can fire on
  symbols Copper imports from LCSC/EasyEDA, which are drawn the way the supplier drew them (4 of about 17,000 stock
  KiCad `U` symbols have an unfilled body; Y1 matches about 1.5% of them).
- Y2 skips stock symbols drawn without a circle on purpose (pre-biased "digital" transistors, thyristors, triacs)
  and parts that do not have exactly three pins.
- C3, C4, C7, F1, F3, G1, G2, P3, Y3, S0 and K1 are guidance only.
- Copper's own engines draw grounds down and every other power symbol, negative rails included, the way the
  stock symbol is drawn (up).
- New projects from `project_create` have inter-sheet references switched on; a project started some other way
  keeps KiCad's default.

## References

- Edward Tufte, *The Visual Display of Quantitative Information* (whitespace, visual flow, chartjunk).
- Horowitz and Hill, *The Art of Electronics*, appendix on drawing schematics.
- IEEE Std 315, *Graphic Symbols for Electrical and Electronics Diagrams*.
- Olin Lathrop, "Rules and guidelines for drawing good schematics", Electrical Engineering Stack Exchange.
