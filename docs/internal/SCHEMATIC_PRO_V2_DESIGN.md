# Schematic "pro2" engine: final design

Status: implemented in four work packages (style `pro2`; `pro` stays the default). Measured results and the
targets that were NOT met are in `bench/sch/out/acceptance.json`; section 14 lists where the implementation
departs from this text.
Scope: `src/copper_kicad/sch/pro/`, `sch/layout.py` (fallback only), the `sch_tidy` tool, `bench/sch`, and the three schematic test files.
Out of scope and not to be touched: `pcb/v2/`, `pcb/guided/`, `tools/route_tools.py`, `pcb/route.py`, `bench/route/`, `tests/test_v2_*`.

Two invariants hold for every step of every work package:

1. The functional netlist is IDENTICAL before and after (engine gate G0-G4 plus the bench's independent kicad-cli comparison).
2. ERC is not worse.

A candidate that fails either is never written. Looks are negotiable; these are not.

---

## 1. Judgement of the three proposals

All three agree on the diagnosis: the current engine places parts greedily around an anchor (`Blk.stem`, `bank`, `attach`), then decides wire-or-label afterwards with `route.MAX_SPAN = 26` cells, and cross-block wiring is effectively off (`route.requests: 0` on c3-devboard). Idioms are accidents of a slide search. All three propose match -> rigid template -> compose. That is adopted. They differ in what surrounds it.

| Proposal | Really removes | Does not remove / weak | Implementability | Netlist risk |
|---|---|---|---|---|
| **idioms** | Non-canonical idioms, detached decap banks, sideways power, most in-block label soup. Best data model (`Match`/`Template`, ports, per-idiom fallback, pin-geometry-solved poses). Per-side 1-D packing replaces the 23-offset slide. | Cross-block label soup (labels whenever a net leaves the block). Text collisions handled only by slots. USB-C overprint conceded. | High. Reuses `Cell`, `pose_pin`, `route.verify`. | Low: templates wire by net key and assert `pin_net` per wire. |
| **flow-and-wires** | Flow direction, connector edges, cross-block label soup, staggered pairs, ambiguous crossings. Best wiring policy (cost thresholds, one label per disconnected group, junction clearance rule) and the only real text solver. | Nothing by itself for idioms beyond what the others have. | Low as written: two-level Sugiyama + Brandes-Koepf + a new channel router + a text solver with layout feedback is four new subsystems, each of which can regress the bench. Three earlier rounds plateaued; a rewrite this wide will not converge in four packages. | Medium: a second router means a second source of accidental shorts. |
| **exemplars** | Gives the score an external reference (85 human sheets) and hard zeros. The union-find pre-gate catches shorts before kicad-cli. Only proposal that actually fixes stacked-pin overprint (`symfix`). | Placement is the least specified. "K candidates x variants" multiplies gate cost. | Score and pre-gate: high. `symfix`: medium, and it requires relaxing G0. | `symfix` is the single riskiest idea across all three: it edits embedded symbols and relaxes the gate that protects them. |

Decisions:

- **Adopt from idioms:** the `Match`/`Template` model, matcher priority with a `used` set, pose solving from real pin geometry, the per-idiom downgrade ladder, per-side 1-D packing, peer-IC facing placement.
- **Adopt from flow-and-wires:** block-level longest-path layering with pinned connector layers; the wire-vs-label cost policy; one-label-per-group; junction/crossing rule; the candidate-based text solver (without min-conflicts and with a single grow iteration); `style_used` reporting per sheet.
- **Adopt from exemplars:** exemplar-calibrated score with hard zeros; the union-find pre-gate; per-pin GND instead of combs; `symfix`, but narrowed (section 8) and last.
- **Rejected:** part-level Sugiyama/Brandes-Koepf (trunk docking in pin order gives straight wires by construction, at a tenth of the code); a new channel router (the existing A* `Router` with rules C1-C7 is kept and given channel reservations and bundle ordering); K-candidate search across template variants (one deterministic candidate per ladder rung, at most 3 gated); hiding or retyping pins; changing any `Value` field.
- **Honest ceiling:** G3 forces every originally named net to keep each name once, so the label count has a floor equal to the number of named nets. Raw catalogue `Value` strings stay (G1). Critics may still dock for these; the design moves the friendly text into frame titles and notes.

---

## 2. Pipeline

```
Design (blocks.py, unchanged net map)
  -> flow.py        net roles, driver/sinks, block quotient DAG, ranks
  -> idioms/        match (read-only) -> instantiate rigid Cells with own wires and ports
  -> dock.py        per IC: idiom cells docked on pin rows, 1-D packing per side; peer ICs facing
  -> page.py        rank columns, frame grid, paper, reserved title-block and notes rectangles
  -> route.py       in-block must-wire, then cross-block by cost; else label
  -> text.py        ref/value/label candidates, hard no-overlap
  -> symfix.py      (WP4, guarded) derived symbols for stacked pins
  -> emit.py        unchanged writer
  -> route.verify_partition   union-find pre-gate (new)
  -> score.py       measure v2
  -> gate.check     G0-G4 on staged, KiCad-resaved bytes; only then write
```

Style ladder in `engine.relayout`: `pro2` -> `pro2-safe` (no cross-block wires, no symfix) -> `pro` (today's engine, kept intact) -> `pro-plain` -> `labels`. At most three candidates are gated per sheet (each gate costs kicad-cli round-trips). The bench reports `style_used` and `tried` per sheet as today; a sheet that silently drops to `pro` counts as a failure of the work package's acceptance check, not as a pass.

All coordinates are on the 1.27 mm unit (`U`); pin tips and wire ends on 2.54 mm wherever the symbol allows (`offgrid_pins == 0` stays gated).

---

## 3. Idiom catalogue

### 3.1 Data model (`sch/pro/idioms/`)

```python
@dataclass
class Match:
    idiom: str        # 'decap', 'divider', 'opamp_stage', ...
    roles: dict       # role -> ref or (ref, unit)
    nets: dict        # net role -> net key  {'IN','OUT','FB','GND','RAIL'}
    host: tuple|None  # (ref, pin) it docks on; None = stage of its own
    score: int        # priority for conflict resolution

@dataclass
class Template:
    parts: dict   # role -> (dx, dy, rot, mirror, field_slot)
    wires: list   # [(net role, [(x, y), ...])]
    ports: dict   # net role -> (x, y, side)
    powers: list  # [(net role, (x, y), 'up'|'down')]   rails up, GND down, nothing else
    labels: dict  # net role -> (x, y, justify)  slot for the one retained name, if the net is named
    flags: dict   # net role -> (x, y)           slot for PWR_FLAGs that belong to this net
    flow: tuple   # (in port, out port)
```

Templates are parametric Python functions. Poses are solved from the actual lib-symbol pin offsets (`pins(si)`, `pose_pin`): a template states "pin 1 of RF lands on port FB" and the instantiator solves rotation/mirror/translation, then asserts that every wire endpoint is exactly a pin tip, a port or another wire vertex of the same net role. Output is an existing `Cell`.

Matching is read-only over the netlist graph and runs in the priority order below; a part is claimed once. Unclaimed parts go to today's `sub_cell`/`stem` path. A wrong match can only produce an odd drawing, never a wrong netlist, because wires are emitted by net key and checked.

### 3.2 Catalogue and canonical drawings

Signal flows left to right in every drawing. "Up" means the symbol points up the page (rails); "down" means GND-style.

| # | Idiom | Match rule | Canonical drawing |
|---|---|---|---|
| 1 | **Boost** | Regulator (`_reg_kind`) with inductor between VIN and SW, diode SW -> VOUT, optional FB divider | One horizontal power line, left to right: VIN port, Cin shunt down, L horizontal, SW node (junction), D horizontal anode-left, VOUT, Cout shunt down, VOUT rail symbol up at the far right. Controller IC below the line, SW pin wired straight up to the SW node. FB divider vertical at the right end of the line, tap wired back to FB. SW is never a label. |
| 2 | **Buck** | Regulator with SW -> L -> VOUT, optional catch diode | VIN left, Cin shunt down, IC in the middle, SW pin right -> L horizontal -> VOUT, catch diode vertical from SW node to GND (cathode up), Cout shunt down, FB divider vertical at far right. |
| 3 | **LDO / 3-terminal** | Regulator with IN, OUT, GND and caps | Cin shunt, U, Cout shunt on one horizontal line; input rail symbol up at left end, output rail symbol up at right end; one GND per shunt, all on one row. |
| 4 | **Op-amp stage** | Per unit: walk 2-pin passives from OUT to IN- | Triangle pointing right. Feedback element(s) horizontal in a loop ABOVE the body from the output node back to the inverting-input node; Rf parallel Cf stacked as two rows of the loop. Gain/ground leg hangs vertically from the inverting node to GND. Input R horizontal on the left on the input pin's row. Follower: a plain wire loop above the body, no label. Output port on the right on the output pin's row. Power unit: drawn beside unit A with its own decaps, rail up, GND down. |
| 5 | **Crystal** | 2- or 4-pin Y between two IC pins, two caps to GND, optional series R | Crystal horizontal between two parallel stubs from XIN/XOUT, placed on the IC side those pins are on; the two load caps drop symmetrically from the two crystal nodes to ONE shared GND symbol centred below; series R inline on the XOUT stub. No labels, no wrap-around. |
| 6 | **USB-C front end** | USB connector, CC pull-downs, ESD array on D+/D-, series Rs | Connector at the sheet's left edge, pins facing right. CC1/CC2 resistors horizontal then down to one shared GND. D+/D- as a parallel pair on adjacent rows through ESD (inline) and series resistors. VBUS straight up to a rail symbol; shield/GND straight down. Stacked pins get one shared stub (and the fan bar of section 8 when symfix applies). |
| 7 | **Divider** | R-R between rail and GND (or two rails), mid node to a high-impedance pin, optional C across the lower leg | Vertical: rail symbol up on top, R top, tap node, R bottom, GND down; tap exits sideways toward its consumer on the consumer pin's row; optional C parallel to the lower R, one column over, sharing its GND row. |
| 8 | **RC / LC filter** | Series element then shunt C to GND | Series element horizontal on the signal row; shunt C drops from the output node to GND. |
| 9 | **LED + R** | Series chain rail <-> pin/GND in any order | Drawn left to right (or top to bottom when docked on a bottom pin): source side first. Anode toward the rail. Resistor and LED colinear, no wire passes through the LED body; rail symbol up, GND down. |
| 10 | **Transistor switch** | Q with base/gate R, pull-down, optional flyback diode | Control enters from the left through the series R; pull-down drops from the gate/base node to GND; load above the collector/drain; emitter/source straight down to GND; flyback diode parallel to the load, cathode up. |
| 11 | **Button** | Switch to GND or rail, optional pull, optional debounce C | Signal node on the host pin's row; switch drops to GND (or rises to rail); pull resistor on the opposite side of the node; debounce C parallel to the switch, one column over. |
| 12 | **Pull-up / pull-down** | Single R from a signal to a rail/GND | Vertical R on the signal wire one column out from the host pin: rail symbol up above (pull-up) or GND below (pull-down). I2C pairs share one row and one rail bar. |
| 13 | **Decap at pin** | C between a rail and GND; assigned to a specific power pin of the graph-nearest IC, counts balanced across same-rail pins, bulk caps to the regulator output | Vertical cap on a short stub off the power pin's own rail wire, within 10 mm of the pin tip. Up to 3 caps per pin group in one row sharing a rail bar on top and a GND bar below with ONE GND symbol; rail symbol up on the bar. Power pins on top of an IC get a short horizontal rail bar directly above them. More than 6 caps on one pin: a compact two-row group still attached to that pin's rail wire. Detached ladder banks are retired. |
| 14 | **Peer IC bus** | Two ICs in a block sharing >= 3 nets (flash on an MCU, ADC on an amplifier) | Peer placed facing the host, mirrored if needed so shared pins are in the same order: N parallel straight wires, no labels. |
| 15 | **Connector breakout** | Whatever remains on a J | Connector at the sheet edge facing inward, one stub per pin on the pin row, names in one aligned label column (see 5.3). |
| 16 | **Module ground** | Module/IC with > 4 GND pins | One GND symbol per pin group of <= 4 adjacent pins (bar with <= 3 junctions), never a comb. |

Each template is tested on at least two symbol variants (e.g. horizontal- and vertical-pin op-amp, 2- and 4-pin crystal).

### 3.3 Per-idiom safety

For each template wire the instantiator asserts that all pins it touches share `pin_net`. On any mismatch or any assertion failure the match is dropped and its parts fall back to the legacy stub-and-label drawing. The bench records `idiom_matches`, `idiom_dropped` and `idiom_coverage` (claimed parts / parts).

---

## 4. Flow ordering and placement

### 4.1 Flow graph (`flow.py`)

- Per net: `role in {gnd, rail, signal, bus_member}`, `driver`, `sinks`. Direction from pin electrical types (existing `Design._flow`); 2-pin passives are two-ports (series between driver and sink, shunt signal-to-rail).
- Power flows input connector -> protection -> charger/regulator -> loads. Rails are not layout edges.
- Feedback cycles are broken by reversing the edge that enters an inverting or FB pin; such edges are tagged `feedback` and never influence rank.
- Output: block quotient DAG with edge weights (number of shared signal nets).

### 4.2 Block ranks

Longest-path layering of the quotient DAG. Pinned: input/power connectors to rank 0, output connectors and headers to the last rank. Bidirectional or debug connectors go to whichever edge their strongest neighbour is nearer. Within a rank, blocks are ordered top to bottom by barycenter of their neighbours in the previous rank (2 sweeps down, 2 up), power above signal as tie-break.

`flow_violations` = number of non-feedback signal edges whose driver block is in a higher rank than (or the driver part is right of) its sink. Target 0.

### 4.3 Inside a block (`dock.py`, replaces the `attach` slide search)

1. The anchor IC is the trunk. Its orientation is chosen so the majority of input pins face left and outputs right; power pins that face sideways get an L-stub to vertical (section 6).
2. Every hosted idiom cell has one port that must land on its host pin's row (left/right sides) or column (top/bottom sides).
3. Per IC side: sort cells by pin order, then solve a 1-D non-overlap packing (longest path on the constraint chain, minimising total displacement from each pin's own coordinate). When a cell cannot sit at pin pitch it is pushed outward to a staggered depth and reached by one straight extension of the pin stub. No dogleg is introduced by docking.
4. Peer ICs (idiom 14) are placed facing the trunk at a gap of `shared nets x 2.54 mm + text allowance`.
5. Stage idioms without a host (boost, LDO, op-amp stage) are their own trunk; consecutive stages in one block are chained left to right with out-port and in-port on the same row.
6. Two-part pairs that connect are aligned to a shared row or column (`staggered_pairs` metric: connected 2-pin parts whose joined pins differ by < 5.08 mm in the off-axis; target 0).

### 4.4 Fallback

A block whose docking fails (overlap unsolvable within the frame limit, or `route.verify` error) is rebuilt with today's `Blk.build`. The rest of the sheet stays on the new path.

---

## 5. Wiring policy and router

### 5.1 Wire versus label

Evaluated per connection (a pair of wire groups of one net), in this order:

| Case | Decision |
|---|---|
| Net internal to an idiom | Always wire (template polyline). Never labelled. |
| GND / rail | Never routed between parts of different idioms. Power symbol at each idiom's designated slot; inside an idiom, shared bars. |
| In-block signal | Wire with `must=True`. Search span = block diagonal (replaces `MAX_SPAN = 26`). Accept if bends <= 3 and added crossings <= 1; otherwise retry once after the dock step adds one 2.54 mm channel; otherwise label. |
| Cross-block signal, blocks in the same or adjacent rank | Wire if Manhattan length <= 60 mm, bends <= 4, added crossings <= 2, net has <= 4 terminals, and cost = length_mm + 8 x bends + 20 x crossings <= 150. |
| Bundle (>= 2 nets with a common prefix or adjacent pins on both ends) | Routed together as parallel tracks ordered by pin row, counted as one decision; wired if the bundle fits the channel. |
| Wide bus (>= 8 members) spanning more than one rank, or anything beyond the thresholds | Labels, in one aligned column at each end (5.3). This is a legitimate use and is not penalised as soup. |
| Hierarchical / global | Label of the original kind, unchanged. |

Targets that follow: `wired_local_ratio >= 0.85` everywhere, `labels_per_signal_net <= 1.0`, `labels_redundant == 0`.

### 5.2 Router (`route.py`, extended, not replaced)

The A* `Router` and rules C1-C7 stay. Additions:

- `MAX_SPAN` becomes a per-call parameter (block diagonal in-block, 60 mm cross-block).
- Symbol bodies, pin-text strips, field boxes and frame title strips are hard obstacles (this is what removes "wire through an LED" and "value under a wire").
- Channel reservation: `page.py` sizes the gap between two rank columns as `(nets crossing the gap) x 2.54 mm + 5.08 mm`; cross-block routes are processed in pin-row order so parallel tracks do not cross.
- Costs: 1 per cell, +8 per bend, +20 per crossing.
- Junction rule: T-junctions only (no 4-way); a crossing must be >= 2.54 mm from any junction and from any pin tip, otherwise the route is rejected and retried on the next track.
- Post-pass `straighten`: remove a dogleg when the two ends can share a row/column by sliding a non-hosted 2-pin part by <= 2.54 mm.

### 5.3 Labels

- One label per disconnected wire group per net name; placed at a wire END, text reading away from the wire along the flow direction, never on top of a segment, never vertical (`labels_vertical == 0`).
- A fully wired named net keeps each of its original names exactly once (G3), at the template's label slot or on a free straight horizontal segment end. An unnamed net gets no label.
- Label columns at connectors and bus ends are left-aligned on one x.
- `labels_redundant` = labels beyond (names x disconnected groups) per net. Target 0.

---

## 6. Power symbols and PWR_FLAG

- GND-class symbols only at rotation "down"; supply rails only "up". `power_sideways == 0` is a hard zero. When an IC power pin faces left/right, the stub is an L: 2.54 mm out, then vertical to the symbol.
- One GND symbol per idiom shunt group, all GNDs of a stage on one row where the template allows.
- Rails are named by symbol only; a rail never gets a local label unless the original sheet had that name on that net (G3).
- **PWR_FLAG:** count per net is preserved exactly (G2); none added, none removed. Each flag is placed upright on the rail wire of its net inside the block where the net is sourced (input connector or regulator output), at the template's `flags` slot, flags of one block on a common row. A flag is never drawn on a stub of its own; if its net has no wire in that block the flag sits directly on the source pin's stub. `flag_islands` (flag whose wire group contains no component pin within the block frame) target 0.
- No-connect markers stay on their pins (G4 `nc_pins`).

---

## 7. Text placement (`text.py`)

Boxes are measured with `model.text_width` plus height, rotation and justification, inflated by 0.3 mm. Unverified assumption to settle in WP4: these advances match KiCad's rendering to within one inflated margin; the check is a tile comparison on three designs.

- **Fields:** each template gives a home slot (ref/value to the right of vertical 2-pin parts, above horizontal ones; ICs: ref top-left above the body, value bottom-left below). Candidates per field: home, mirrored side, above, below, plus two at +1.27 mm. Ref and value move as a stacked pair first.
- **Labels:** 2 ends x 2 justifications.
- **Power symbols:** fixed; they are obstacles.
- **Solver:** greedy, most-constrained first. Hard constraints: no overlap with symbol bodies, pin name/number strips, wires, junctions, other text, frame title strips. Soft cost: distance from home. One repair pass re-seats the neighbours of any unplaced text. If something is still unplaced the solver asks `dock.py` for +2.54 mm on that row or column once and re-runs; after that the overlap is reported in `hint` and counted.
- **Pin names inside bodies** that collide because of rotation: prefer the trunk orientation in which the symbol is at rotation 0; if the symbol itself overprints at rotation 0 it is a library defect and goes to section 8.
- Hard zeros: `overlaps == 0`, `wire_through_text_or_body == 0`.

---

## 8. Symbols with overprinting pin text (`symfix.py`)

Problem: USB-C style symbols stack pins (A1/B12, A4/B9 ...) at one coordinate, so the pin numbers overprint. Pose changes cannot fix that.

Level 1, always on, no symbol edit:
- Detect coincident pin tips in the embedded lib symbol. Give the stack a single shared stub; reserve the pin-number strip as a hard text and wire obstacle; emit a `hint` naming the symbol and pins.

Level 2, `symfix="auto"`, last work package, guarded:
- Write a derived embedded symbol `<name>_cu` in which the 2nd..nth pin of each stack is moved to adjacent rows (2.54 mm pitch), body rectangle grown to fit. The FIRST pin of every stack keeps its original tip coordinate.
- The instance keeps its `lib_id` and all fields; only `lib_name` points to the derived symbol.
- The template draws a short vertical bar joining the fanned tips, and the bar passes through the original tip. Consequence: if the user later runs "Update symbols from library" the pins fall back onto a point that is still on the bar, so connectivity is preserved either way.
- Gate change (the only change to `gate.py` in this design): G0 accepts a lib-symbol hash change only when (a) the symbol name ends in `_cu` and was produced in this run, (b) the sorted tuples of every pin (number, name, electrical type, shape, unit, body style, hidden flag, alternates) are equal to the original's, (c) all symbol properties and `in_bom`/`on_board`/`power` flags are equal, and (d) the instance `lib_id` and fields are unchanged. G1-G4 unchanged. The independent kicad-cli netlist comparison in the bench is unchanged and remains the arbiter.
- ERC: for a symfix candidate, `lib_symbol_mismatch` and `lib_symbol_issues` are NOT exempted as geometry types. If either count rises on the staged copy, the engine falls to Level 1 for that symbol. "ERC not worse" is therefore honoured literally.
- Never done: hiding pins, retyping pins, merging pins, editing library files on disk.

If Level 2 turns out to raise an ERC warning in every realistic environment, it ships disabled and the overprint remains a reported library limitation. That outcome is acceptable; a relaxed gate that lets a real change through is not.

---

## 9. Page composition, frames, title block, notes (`page.py`, `notes.py`)

- **Grid:** frames sit on a 2.54 mm super-grid; rank columns share left edges; frames in one column share width; frames that start a row share top edges. Gutters are equal (5.08 mm between frames, channel width per 5.2 between ranks). `frame_ragged` = distinct left-edge x values minus rank columns, plus distinct widths per column minus 1; target 0.
- **Border:** content at least 10.16 mm from the drawing border; nothing touches it (`border_min_mm >= 10`).
- **Connectors at edges:** connector blocks are in the first or last rank column, connector body on the outer side of its frame facing inward. `connector_edge_ratio` = connectors (>= 2 pins, not test points) whose block is in an outer column / all such connectors; target 1.0.
- **Paper:** smallest of A4..A0 on which the layout fits with `sheet_fill >= 0.60` (content bounding boxes of frames / drawable area minus the title-block rectangle). If the next smaller paper fits at <= 0.85 fill it wins. Slack is distributed evenly into inter-rank channels first, then gutters; content is centred (`off_centre <= 0.05`).
- **Reserved rectangles:** title block corner (bottom-right) and a notes frame (bottom-left, or right column above the title block when the bottom row is full).
- **Frame titles:** `FUNCTION  detail` as today, with the friendly connector name there (e.g. "USB-C  POWER INPUT") because `Value` cannot change (G1).
- **Title block:** title = project name; rev and date kept if present, otherwise date filled; comment 1 = one-line function summary derived from blocks ("USB-C powered ESP32-C3 board, 3V3 LDO, GPIO header"); comment 2 = rails; the machine "tidied by Copper" line moves to comment 4. Title-block text is not part of the netlist; G1 field comparison is unaffected.
- **Auto design notes** (`notes.py`, plain text items, deterministic, derived only from the netlist and idiom matches; a note whose inputs cannot be parsed is omitted, never guessed):
  - rails and their sources ("+3V3 from U2 (AMS1117-3.3), input VBUS");
  - regulator output from the FB divider, shown as the formula with part refs and the computed value only when the reference voltage is known from a table for that part family, otherwise formula only;
  - RC corner frequency for filter idioms, LED current for LED idioms at the rail's nominal voltage (assumed Vf stated in the note);
  - crystal frequency and load-cap values; I2C pull-up values and rail;
  - connector friendly names and pinout summary;
  - "place Cx close to Ux pin n" for each decap group.
  - Per-block notes sit inside the block frame under the title when there is room; the rest go to the notes frame. `notes_count >= 2` per sheet with >= 8 parts.
- Existing user text is preserved and parked as today (`_park_notes`).

---

## 10. Netlist-identity and ERC gates

Layered, cheapest first; every layer can only reject.

1. **Template assertions** (section 3.3): per wire, all touched pins share `pin_net`.
2. **Union-find pre-gate** `route.verify_partition(sch, D)` (new, pure Python, no kicad-cli): union wires, junctions, pin tips, labels and power symbols by coincidence exactly as KiCad connects them (wire end on wire interior only with a junction; pin tip on wire end or interior per KiCad rules; labels by name; power symbols globally by value). The result must reproduce (a) the pin -> net partition of `Design`, (b) the per-net name inventory, (c) PWR_FLAG count per net, (d) the no-connect pin set. It also asserts that no wire end, label anchor or pin tip of net A lies on geometry of net B. The existing `route.verify` stays and runs after each block build.
3. **Engine gate** `gate.check`, unchanged except the narrow G0 clause of section 8: G0 symbol payload, G1 fields, G2 flags, G3 name inventory, G4 special items and NC pins, on the staged, KiCad-resaved bytes; whole-project canonical netlist hash equal.
4. **ERC:** `erc_new(before, after)` must be empty; per-net types (`power_pin_not_driven`, `pin_not_driven`) compared per net; geometry-type exemptions as today, minus the symfix exception above.
5. **Bench arbiter:** `bench/sch/run.py` independent kicad-cli netlist comparison and ERC before/after, unchanged. `netlist_identical` must be true on 7/7 and `erc.new` empty on 7/7 after every work package.

Failure handling: idiom -> legacy drawing for those parts; block -> legacy `Blk.build`; sheet -> next rung of the style ladder. Nothing is written unless the last rung reached passes layers 2-4. A pre-gate rejection of a candidate that the engine gate would have accepted is a bug to fix, but is safe.

---

## 11. Quantitative score (`score.py` v2)

`measure()` keeps every existing key (bench and tests read them) and adds the new ones. Exemplar constants (p50/p75/IQR of human sheets) are produced by `bench/sch/exemplars.py` from the KiCad demo schematics shipped with the local install (read-only) and checked in as Python constants in `sch/pro/exemplar.py`, so nothing is read at runtime and packaging stays stdlib-only. Starting values, to be re-measured in WP1: labels/part p50 0.85, p75 2.09; nearest-neighbour part distance p50 10.2 mm, p75 17.8 mm; sheet fill p50 0.61, p75 0.82; PWR_FLAGs/sheet p90 3; notes/sheet p50 2.

**Hard zeros** (each violation is listed in `hard`; a candidate with any hard violation ranks below every candidate without, and the work-package checks require an empty list):

`overlaps`, `wire_through_text_or_body`, `power_sideways`, `labels_vertical`, `labels_redundant`, `crossing_near_junction`, `offgrid_pins`, `flag_islands`, `border_min_mm < 10`.

**Score** = 100 - sum of penalties, clamped to [0, 100]:

| Term | Penalty | Cap |
|---|---|---|
| Wiring | 60 x max(0, 0.85 - wired_local_ratio) | 25 |
| Label density | 6 x max(0, labels_per_part - 0.85) / 1.24 | 12 |
| Idiom coverage | 20 x max(0, 0.6 - idiom_coverage) | 8 |
| Decap distance | 0.8 x max(0, decap_dist_mean_mm - 8) | 10 |
| Satellite distance | 0.4 x max(0, sat_dist_mean_mm - 17.8) | 6 |
| Flow | 3 x flow_violations | 9 |
| Connector edges | 8 x (1 - connector_edge_ratio) | 8 |
| Crossings | 1.5 x wire_crossings | 6 |
| Bends | 4 x max(0, bends_per_wire - 1.0) | 6 |
| Staggered pairs | 1 x staggered_pairs | 4 |
| Fill | 30 x max(0, 0.60 - sheet_fill) | 8 |
| Frames | 1 x frame_ragged + 20 x max(0, off_centre - 0.05) | 5 |
| Notes / title | 3 if notes_count < 2, 2 if title-block fields filled < 3 | 5 |
| GND combs | 1 x max(0, max_junctions_on_one_power_net - 4) | 4 |
| Hard violations | 5 each | 30 |

The old score is still reported as `score_v1` for one round so the two can be compared. The score is a regression guard and a ranking device; the human-standard critic pass on the PNG tiles remains the real acceptance test, and no claim of "8.5/10" is made from the number alone.

---

## 12. Work packages (sequential)

Every package ends with: `python bench/sch/run.py` (7 designs), then `tests/test_schematic.py`, `tests/test_schematic_pro.py`, `tests/test_schematic_pro2.py` and the new `tests/test_schematic_pro3.py`, one file at a time, with scratch KiCad config dirs. Common gate for all four: `netlist_identical` 7/7, `erc.new` empty 7/7, all existing tests green, `style_used == "pro2"` on 7/7 (a fallback is a failure to explain, not a pass). Baseline for comparison is the current `bench/sch/out/summary.json` (e.g. c3-devboard: labels 30, wired_local_ratio 0.60, decap_dist_mean 27.3 mm, power_sideways 3, sheet_fill 0.509; li-ion-powerbank: labels 24, power upright 24/31, 6 PWR_FLAG; rp2040-min: 69 labels).

### WP1. Measure, guard, and passive idioms

Files: `sch/pro/score.py`, `sch/pro/exemplar.py` (new), `sch/pro/route.py` (`verify_partition` only), `sch/pro/idioms/__init__.py`, `idioms/model.py`, `idioms/match.py`, `idioms/passive.py` (new), `sch/pro/build.py` (hook idiom cells into `Blk`, banks/stems become fallback), `sch/pro/engine.py` (style `pro2`, ladder, pre-gate call), `bench/sch/exemplars.py` (new), `bench/sch/run.py` (report new metrics, `hard`, idiom stats), `tests/test_schematic_pro3.py` (new).

Tasks: score v2 with exemplar constants and hard zeros; union-find pre-gate with negative tests (injected short, dropped label, moved flag must each be caught); `Match`/`Template`/instantiator with assertions and per-idiom fallback; idioms 7-9, 11-13 and 16 (divider, RC, LED+R, button, pull, decap-at-pin, module ground); power-symbol up/down policy with L-stubs.

Acceptance on bench/sch: `power_sideways == 0` on 7/7; `decap_dist_mean_mm <= 10` on 7/7 and <= 8 on at least 5; `max_junctions_on_one_power_net <= 4` on 7/7; no LED chain drawn right-to-left (`flow_violations` restricted to LED idioms == 0); `idiom_coverage >= 0.4` mean; the pre-gate agrees with `gate.check` on all 7 (no false rejects) and catches all injected faults in the test file.

### WP2. IC idioms, trunk docking, in-block wiring

Files: `idioms/stage.py` (new: boost, buck, LDO, op-amp, crystal, USB-C, transistor, peer bus), `sch/pro/dock.py` (new), `sch/pro/build.py` (`Blk.build` delegates to dock; legacy path kept as fallback), `sch/pro/route.py` (span parameter, body/text obstacles, bend and crossing costs, junction rule, `straighten`), `sch/pro/engine.py` (in-block label policy, one-label-per-group), `tests/test_schematic_pro3.py`.

Tasks: idioms 1-6, 10, 14 with two symbol variants each in tests; per-side 1-D packing; peer-IC facing placement; in-block must-wire with the 5.1 thresholds; redundant-label removal under G3.

Acceptance on bench/sch: `wired_local_ratio >= 0.85` on 7/7; `labels_redundant == 0` on 7/7; opamp-afe has zero labels on the feedback and follower nets inside the amplifier block (asserted by net name in the test); li-ion-powerbank boost block has VIN, L, SW, D, VOUT x-coordinates strictly increasing and no SW label; rp2040-min crystal net fully wired with one shared GND, and labels <= 40 (from 69); `wire_through_text_or_body == 0` and `crossing_near_junction == 0` on 7/7; `staggered_pairs == 0` on at least 5/7.

### WP3. Flow ranks, page composition, cross-block wiring

Files: `sch/pro/flow.py` (new), `sch/pro/page.py` (new; replaces `engine.compose` / `_compose_paper`, which remain for the `pro` rung), `sch/pro/engine.py` (wire in flow + page, cross-block routing pass, PWR_FLAG slots), `sch/pro/route.py` (channel reservation, bundles), `sch/pro/blocks.py` (expose driver/sink and connector direction; detection logic otherwise unchanged), `bench/sch/run.py` (frame/flow metrics), `tests/test_schematic_pro3.py`.

Tasks: net roles and quotient DAG; rank layering with pinned connector ranks and barycenter ordering; rank-column frame grid, channel sizing, paper choice, centring, reserved title/notes rectangles; cross-block wiring by cost with bundle ordering; label columns; PWR_FLAG placement on source rail wires.

Acceptance on bench/sch: `flow_violations == 0` on 7/7; `connector_edge_ratio == 1.0` on 7/7; `sheet_fill >= 0.60` on 7/7; `frame_ragged == 0`, `border_min_mm >= 10`, `off_centre <= 0.05` on 7/7; `flag_islands == 0` with PWR_FLAG count per net unchanged; `labels_per_signal_net <= 1.0` on 7/7 and rp2040-min labels <= 28; `wire_crossings` not higher than the WP2 result on any design.

### WP4. Text, symbols, notes, title block, critic round

Files: `sch/pro/text.py` (new), `sch/pro/symfix.py` (new), `sch/pro/gate.py` (the single G0 clause of section 8 and the symfix ERC rule; nothing else), `sch/pro/notes.py` (new), `sch/pro/page.py` (notes frame, title block fields), `sch/pro/emit.py` (derived embedded symbol, `lib_name`; text items for notes), `sch/pro/tool.py` and the `sch_tidy` tool description (`style`, `symfix` options, new report fields), `sch/layout.py` (only if the `labels` rung needs the upright-power rule; otherwise untouched), `docs/ARCHITECTURE.md` (pointer to this document), `tests/test_schematic_pro3.py`.

Tasks: text candidate solver with the single grow iteration; symfix Level 1 everywhere, Level 2 behind `symfix="auto"` with the equivalence gate and its negative tests (a derived symbol with one changed pin type, one renamed pin, one hidden pin must each be rejected); auto design notes and title-block fill; frame titles carrying connector friendly names; final tuning against critic tiles.

Acceptance on bench/sch: `hard` list empty on 7/7 (`overlaps == 0`, `wire_through_text_or_body == 0`, all other hard zeros); on designs with a USB-C connector, either zero coincident pin-number boxes in the after sheet (Level 2 applied, gate and ERC clean) or Level 1 with an explicit `hint`, and the summary states which; `notes_count >= 2` and title-block fields filled >= 3 on 7/7; score v2 >= 90 on 7/7; then a strict human-standard critic round on after-PNGs and tiles with a target mean >= 8.0 and no design below 7.5. The critic number is the result to report, whatever it is.

---

## 13. Risks

- **Matcher ambiguity** (pull-up versus divider top, decap versus filter cap): priority order plus fallback keeps the netlist right; the drawing may vary. Mitigation: matcher unit tests on the seven designs plus three or four unseen ones before any score claim.
- **Overfitting to seven designs:** add unseen designs to `bench/sch/corpus` in WP3 at the latest; report them separately.
- **Symbol variety:** poses are solved from real pin geometry, but a symbol with pins on unexpected sides drops the idiom to fallback. `idiom_dropped` makes this visible.
- **G3 label floor and G1 value strings:** cannot be designed away; the bench reports `labels_floor` (named nets) beside `labels` so critics see what is forced.
- **symfix:** the only gate relaxation. It is last, opt-out, strictly pin-equivalent, reverts safely, and is disabled if ERC shows any new item.
- **Runtime:** pre-gate is pure Python; at most three kicad-cli gated candidates per sheet.
- **Text metrics:** `model.text_width` versus KiCad rendering is unverified until WP4's tile comparison.

---

## 14. Where work package 4 departs from this document

- **symfix Level 2 replaces the embedded symbol in place** (same `lib_symbols` key) instead of adding a `<name>_cu`
  symbol selected with `lib_name`. Measured on KiCad 10.0.6: with `lib_name` kicad-cli writes the new name into
  the component's `libsource` and into `libparts`, so G1 refuses every such candidate. In place, the exported
  netlist is identical. The G0 clause is therefore "a changed embedded symbol is admitted when its name, pins
  (number, name, type, shape, unit, body style, hidden, alternates, length, direction, text style) and all
  properties / flags are equal"; a `lib_name` redirect is always refused.
- **Two defects are handled**: visible stacked pins (fanned to free neighbouring rows, first pin keeps its tip) and
  pin numbers wider than their pin (the USB-C symbols of the corpus: one pin numbered "A1B12"), fixed by a wider
  `pin_names` offset when both sides' names still fit. No bar is drawn by symfix: neighbouring pins of one net
  already get the engine's jumper.
- **ERC rule**: library-symbol checks are not exempt for the parts whose symbol was redrawn (per item, not as a
  total: the number of power symbols changes with every layout). In an environment without the part's library
  (the bench) the check cannot fire; with the library installed `lib_symbol_mismatch` is expected to refuse
  Level 2, which then falls back to the hint. That case is covered by a unit test of the rule, not by a run.
- **Text solver**: runs on the finished sheet and moves only fields that touch something. The "grow" iteration is a
  second ring of candidates 2.54 mm further out, not a request to `dock.py` for more room. Labels and power
  symbols are not moved by it.
- **Power symbols**: a side pin whose neighbouring rows are wired gets an L that crosses at most two wires squarely
  (never within 2.54 mm of a wire end or junction). A pin that would need three crossings keeps a sideways symbol.
- **Vertical names**: a name at the end of a vertical wire is turned horizontal, the wire running on until there is
  room beside it.
- **Title block**: comment 1 = what the sheet is, comment 2 = rails, comment 4 = the line saying both are
  generated (which is also how the next run recognises them as its own).

---

## 15. Fix round 1 after the critic round (what changed against sections 5, 9 and 11)

Three strict critics scored the work-package-4 output 6.6 / 10. The causes that were fixed, and where they depart from
the text above:

- **Frames are as large as their drawing** (section 9 said: frames of a column share one width, slack goes into the
  frames). Stretched frames read as "oversized, mostly empty". Now `page.GROW_H = 1.0`, `GROW_W = 1.1`; a frame
  narrower than `NARROW` x its column keeps its own width, and neighbouring narrow frames of a column share a row
  (`page._shelve`): the supply input stands beside its regulator above the wide MCU frame. A frame that holds a
  connector keeps the outer end of its row; a plain output connector's frame stands on the right edge of the last
  column. The notes of a frame get their own strip under its drawing (`engine.note_room`), bottom-left; when the
  sheet would need the next paper size for that, the notes go where room is left, as before. `frame_ragged` is
  therefore no longer 0 and `sheet_fill` is lower on sheets whose paper is larger than the circuit needs - both are
  still measured and reported as they were. `tests/test_schematic_pro3.py::test_frames_stand_on_a_column_grid` holds
  the new rule.
- **The supply chain reads down one column** and stays above what it feeds (`page.compose` cost terms).
- **No wire ring around a part** (section 5.1 said: in-block signal = wire whenever the bend / crossing limits allow).
  Two pins of one IC that face different ways (the in / out pins of a TVS array) are named on both sides; a wire from
  an IC pin to something that lies behind that pin (a reset line under the MCU) is two names. A strap across the
  doubled contacts of a small switch stays a wire, and the text solver keeps the Reference out of the strap.
  `wired_local_ratio` counts pin pairs, so these sheets fall below 0.85 on purpose; the per-design guards are in the
  test file (`WIRED_MIN`).
- **No junction under a name**: a name that stands on its wire reserves the wire under its letters
  (`Router.no_tee`); a name that would start on a junction moves along its wire.
- **Net names need air**: the text solver treats a field closer than 0.25 mm to a label (0.6 mm more above an
  overbar) as touching, and re-seats it.
- **Notes and title block**: no line is cut off half way (a pin list or a cap list that does not fit is replaced by a
  count), the 555 astable gets its frequency instead of a wrong "low-pass", the one-line summary drops part numbers
  from the end instead of "+ 1 more", and the engine's own marker line moved from comment 4 to comment 9, which the
  standard drawing sheet does not print (a sheet of an earlier version is migrated).
- `connector_edge_ratio` looks at the frame a connector is drawn in (a debug header that was given a frame of its
  own at the sheet's edge was counted with the IC it serves).

**Second pass of work package 2 (after fix round 1).** The "no wire ring" rule stays for the ROUTER (a routed ring is
a spiral around the part and its texts). A pass-through part is now a drawing of its own instead
(`build.Blk._bridges`, switch `build.PASS_THROUGH_BAR`): a part with 5 to 8 pins whose left and right pin of one row
are on one signal net gets a bar over (top row) or under (bottom row) the part, the supply / ground pin between the
legs crosses its bar squarely and its symbol stands straight beyond it, Reference and Value move right of the legs.
It is only drawn when the part can be posed with ground down and supply up; `dock.dock_anchor` prefers that pose when
every facing pin still meets a row the host offers, and a doubled pin pair of the host (USB-C D+ on A6 / B6) offers
its second row for that (`Cell.alts`). The net is then one wire with one name: c3-devboard, phone-breathalyzer and
rp2040-min are back at `wired_local_ratio` >= 0.85 with two names fewer each, at the price of 2 to 3 more crossings
per sheet. Whether a reader prefers the bar to two names has NOT been put to a critic round; setting
`PASS_THROUGH_BAR = False` restores the fix-round-1 drawing.

Not done in this round (still open): decoupling caps at the pins they serve (`decap_dist_mean_mm` unchanged), the
upright-power hooks on USB-C / charger pins, the 4-pin crystal ground ring, PWR_FLAG clutter, pin-number overprint of
library symbols, the 555 astable and the buck feedback as canonical drawings, connector orientation inside the
supply-input frames, and a frame that is wider than its drawing because stale label reservations count as content.



## Owner requirement added 2026-11-11: default symbols first (binding)

The product owner requires: "use the default symbols when you can." In this engine that means:

- Never replace a KiCad stock-library symbol with a generated or alternate symbol to fix a drawing problem
  (overprinting stacked pin numbers and similar). Solve those with placement, pin-text settings that KiCad itself
  offers on the instance, or leave them and report them. Generated symbols are a last resort for parts that have no
  stock symbol at all, and each use is reported.
- Where a part currently uses an imported (EasyEDA-converted, `copper-lcsc:`) symbol and a verified pin-compatible
  KiCad stock symbol exists (same pin numbers, compatible pin names and electrical types, same unit count), prefer the
  stock symbol. Swapping is only allowed when the netlist gate proves the netlist identical afterwards, and is
  reported per reference; it is never done silently.
- Passives, LEDs, diodes, transistors, connectors and power symbols always use the standard `Device:`, `Connector*:`,
  `power:` and similar stock symbols.

**Second pass of work package 3 (after fix round 1).** One change to the drawing, and a record of what the
acceptance list of section 12 cannot reach as the engine stands:

- **A rail's PWR_FLAG stands on the rail's own wire** (`build.Blk.add_flag`, switch `build.FLAG_ON_RAIL_WIRE`): upright
  on a free spot of a horizontal wire of that rail in the source block (the wire from a receptacle's GND pin to its
  ground symbol); the side wire beside the supply symbol is the fallback, as before. Flags are placed before the
  router runs, so a flag can take the lane a signal wire needs: `engine.relayout` therefore draws the `pro2` rung
  both ways in memory and keeps the drawing with the flags beside their symbols when that one has fewer names or
  fewer crossings (phone-breathalyzer: the USB_DM wire to the TVS bar). Count per net is unchanged (G2).
- **Not reachable, and why** (measured on the bench, see the acceptance file):
  `labels_per_signal_net <= 1.0` and "rp2040-min labels <= 28" contradict G3 and section 5.1: rp2040-min has 35 named
  nets (`labels_floor`), and a header bus drawn as two label columns costs two names per net by design.
  `sheet_fill >= 0.60` and `frame_ragged == 0` contradict fix round 1 ("a frame is as large as its drawing"): the
  sheets below 0.60 do not fit the next smaller paper, and their bounding box is set by the tallest frame.
  `off_centre <= 0.05` fails on the two A4 sheets whose widest column stands over the title block and fills the
  height above it (0.065); the alternative is A3 at a third of the fill.

**Second pass of work package 4 (after fix round 1 and the owner requirement above).** Text and symbol rules only;
the acceptance numbers of section 12 that depend on placement (score v2 >= 90, `decap_dist_mean_mm`, the two
sideways supply symbols of stm32-sensor) did not move and are reported as missed.

- **symfix Level 2 never touches a stock or user library symbol** (`symfix.may_redraw`, `GENERATED_LIBS`): it is
  confined to the symbols Copper generated itself (library nickname `copper-lcsc`, the EasyEDA conversions). The
  engine does not offer the `pro2+symfix` candidate for anything else, `symfix.apply` skips it, the hint says the
  symbol is left as its library draws it, and the G0 clause (`gate.symfix_ok`) refuses a changed embedded copy of a
  non-generated symbol even when it is pin-equivalent - the clause got narrower, not wider. Test:
  `test_symfix_never_redraws_a_stock_symbol`.
- **A name does not stand over a junction that a part pin makes** (`engine._name_on_wire`, new `tips` argument): a
  filter cap whose pin ends ON the wire puts a junction dot there exactly as a tee wire does; the name of the net
  moves along the run ("AIN_F", "LPF" on opamp-afe stood struck through by the dot).
- **Words keep apart** (`text.WORD_AIR`, `text._words_touch`): a Reference / Value closer than 0.5 mm to the value
  text of a power symbol or PWR_FLAG on the same line, or to another part's field, is a conflict the text solver
  re-seats ("PWR_FLAG330n"). `build.Blk.words_apart` (`WORD_GAP`) keeps the value text of a new upright power symbol
  off the text line of a neighbouring one ("GND VBUS" beside a receptacle read as one word), and a PWR_FLAG on a side
  wire stands `TEXT_AIR` further from its supply symbol. Widening the boxes of ALL power symbols by the same air
  was tried and reverted: it took routing lanes (wired_local_ratio fell on four designs).
- **The sheet's NOTES block starts on the left edge of a frame column** (`page.notes_rect`): the leftmost column
  with room under it; the largest free rectangle is only the fallback (the block floated between two columns).
- Tried and reverted: giving a frame that holds a right-edge connector its own width instead of the column's (the
  ATtiny frame of motor-driver is half empty). Right-aligned it breaks `flow_violations == 0`, left-aligned
  `connector_edge_ratio == 1.0`; the stretched frame stays until the block can be mirrored.


---

## 16. Fix round 2 (2026-11-11): what changed after the second critic round

Three strict critics scored the output of section 15 at 6.9 / 10 (netlists identical on 10 / 10). Their defects were
grouped by cause; the causes below were fixed in the engine. The gate (`gate.py`) and the bench are untouched; netlist
identity and "ERC not worse" hold on all ten bench designs after the round.

| Cause | Change | Where |
|---|---|---|
| The bar around a pass-through TVS array read as a frame around the part and crossed its supply pins | `PASS_THROUGH_BAR = False`: D+ / D- run straight through the part, a wire on the connector side and the name on the far side | `build.py` |
| The cost allowance of an in-block route also paid for bends and crossings, so a wire with two bends could spend all of it on length (NRST under the crystal, MISO over two supply symbols, RS485_B around its termination) | A named net between two different parts of three or more pins is wired only when the wire is at most `route.WRAP` (4) cells longer than the straight way; otherwise both ends carry the name. A strap across ONE part's contacts and a wire to a two-pin part are exempt (an island of two names would be worse) | `route.route_nets(tight=)`, `engine.tight_pair` |
| A name could land on a symbol that was drawn into its row later | The stub runs on until the name stands free (`engine.name_net`); where there is no room at all (a sideways supply symbol next to the pin) the wrap rule above is not applied to that pin | `engine.py` |
| A pin next to a pin that hosts a stem of the same net was routed around whatever hangs on the stem (TRIG / THRES of a 555) | One short link between the two stubs, drawn after the stem | `build.Blk.place` (`ties`) |
| The doubled ground contact of a tactile switch got a ground symbol at each end, the Value text squeezed between them | One bar under the part, one symbol in its middle (`Blk._rail_pairs`); the text solver keeps Reference / Value 0.6 mm off supply / ground graphics on their line, off no-connect crosses, off a wire that runs along the text, and off the part's own pins and graphic for parts of up to six pins | `build.py`, `text.py` |
| A DC jack that feeds a rail through a Schottky was "I/O": pins looking out of the sheet, its diode an island in the consumer's frame | `blocks.power_feed`: a connector with at most two signals whose net reaches an unsourced rail through F / D / FB / L is a supply input; its protection part is drawn in its frame, left to right | `blocks.py` |
| Connectors pinned to the first / last column looked the wrong way; a supply connector with nothing but rail pins was never turned | `Blk.face_hint` from `flow.pin`; `Blk.place` counts the rail pins when there are no signal pins | `engine.py`, `dock.py`, `build.py` |
| A lone CC pull-down hung through the D+ / D- rows of the receptacle and blocked the straight wires to the TVS array | The resistor lies in the CC row and its ground drops beyond it (`passive.pull_inline_template`); `dock.dock_rows` lets a docked row cross ONE such vertical wire squarely (2.54 mm from its ends, nothing else within 2.54 mm) | `passive.py`, `build.Blk.try_node`, `dock.py` |
| Pull-ups that found no room at their pins stood as islands at different heights | They stand together: one row, one pitch (`dock.build_block`, `pull_rail`) | `dock.py` |
| The cap groups above an IC formed a staircase | The groups that are drawn above the part share one row, supply symbols on one line (`BANK_ONE_ROW`, wraps at `BANK_ROW_MAX`) | `build.Blk.add_banks` |

Measured consequences (bench/sch, before -> after): labels c3-devboard 26 -> 28, opamp-afe 11 -> 14, phone-breathalyzer
19 -> 21, rp2040-min 58 -> 60, rs485-node 29 -> 31, ne555-blinker 8 -> 7; wire crossings c3 6 -> 4, opamp-afe 3 -> 0,
breathalyzer 3 -> 2, rp2040 7 -> 5; `wired_local_ratio` falls where a ring or a wrap became two names (c3 1.0 -> 0.76,
rp2040 0.87 -> 0.72, rs485-node 0.67 -> 0.54, opamp-afe 1.0 -> 0.84, breathalyzer 0.93 -> 0.80), and with it score v2
on those sheets (c3 72.9 -> 66.9, rp2040 69.0 -> 61.2, rs485 72.5 -> 64.7). That is the price of the critics' rule and
is NOT compensated in the score. rp2040-min `decap_dist_mean_mm` 30.6 -> 32.9 (the one row of cap groups stands 2 mm
further from the pins than the staircase did). The regression guards of `tests/test_schematic_pro3.py`
(`LABELS_MAX`, `WIRED_MIN`, `DECAP_MAX`) were moved to these values and say why.

Still open after this round (seen on the sheets, not fixed):

- supply / ground symbols reached by an arch where the rows next to a side pin are wired (TP4056 TEMP / EP / VCC,
  USB-C B4A9, ESP32 GND pins 1 / 2); the two sideways symbols of stm32-sensor (VDDA / VSSA between the crystal and
  NRST), which also keep the NRST wire around the crystal;
- decoupling caps that stand above the part instead of at the pin they serve (li-ion C1, stm32 C3, ne555 C1, most of
  rp2040-min), `decap_dist_mean_mm` far from the 10 mm target;
- pin-number overprint of library symbols ("A1B12" on the 2.54 mm pins of the generated USB-C symbol: the gate does
  not allow a pin's length to change), no-connect crosses on short pins, DW01A / LM386 pin names inside the body;
- labels that G3 forces on private two-pin nets (LED_A, LED_K, GAIN_A ...), PWR_FLAGs that G2 forces to stay;
- the four-pin crystal's ground ring, the LM4040 drawn horizontally, the Zobel network of the LM386, the 120 R
  termination drawn in line with RS485_A, the QSPI bus half wired / half named (the flash's left pins look away);
- sheet composition: rp2040-min and phone-breathalyzer need A3 and fill a band of it, audio-amp and ne555-blinker are
  small on A4, the ATtiny frame of motor-driver is half empty, the NOTES block has no fixed place.


---

## 17. Fix round 3 (2026-11-11): what changed after the third critic round

Three strict critics scored the output of section 16 at 7.3 / 10 (netlists identical on 10 / 10). The causes below
were fixed; the gate (`gate.py`) and the bench are untouched; netlist identity and "ERC not worse" hold on all ten
bench designs after the round. Every change has a module switch, named in the table.

| Cause | Change | Where |
|---|---|---|
| A sideways supply symbol next to a pin excused a wire AROUND the part (NRST of stm32-sensor ran 120 mm around the crystal) | The excuse is gone (`engine.SIDE_SYM_TIGHT`); every other named wire may be at most `route.LOOSE` (8) cells longer than 1.4 x the straight way, the bend / crossing allowance can no longer be spent on length | `engine.tight_pair`, `route.route_nets` |
| ... and the name then had no room at the pin (the crystal's drop stood 5 cells from the pins, the sideways symbol in between) | A stage that is drawn at the pins (crystal) first tries to keep the way out of the waiting side supply / ground pins free (`build.RAIL_LANE` cells, `Blk.rail_lanes`); only when that fails it is placed as before | `build.Blk.place`, `dock.try_crystal` |
| The doubled contacts of a 4-pin tactile switch were closed by wire rectangles (a strap over the part, a ground bar under it) | Each ground contact gets its own ground symbol on a short L (`build.RAIL_PAIR_BAR = False`); the far signal contact carries the net's name instead of a strap (`engine.SWITCH_NAMES`) | `build.Blk._rail_pairs`, `engine.skip_pair_v2` |
| The case pins of a 4-pin crystal were taken to the shared ground bar: a ring of wire around the crystal | The case pin that lies above its side's signal pin ends in a ground symbol of its own after its one square crossing (`stage.OWN_GROUND`); the pin below still joins the bar | `idioms/stage.crystal_template` |
| A bus between two parts was drawn two ways (QSPI: three wires, three names) | A bus (three or more names with one prefix between the same two parts of five or more pins, `route.bus_prefix`) is wired completely or named completely: `dock.BUS_WHOLE` at the facing placement, `engine.BUS_ALL_OR_NONE` re-wires the sheet once without the bus when the router wired only part of it | `dock.dock_anchor`, `engine._half_wired_buses` |
| The name of a net wired across two frames stood on a frame border | `_name_on_wire` keeps 1.5 mm clear of every frame's vertical borders | `engine.py` |
| "Place Cx close to Uy" under every frame read as boilerplate | Not written any more (`notes.DECAP_NOTES = False`); the computed notes (LED current, divider, crystal load, pinouts, rails) stay | `notes.py` |

Tried and reverted in this round: trying a square crossing of one wire BEFORE an arch to a supply symbol
(`build.ARCH_LAST`, left in as a switch, off). On li-ion-powerbank it produced a sideways ground symbol and one more
crossing and removed no arch: the arch is chosen before the stems are drawn, when there is nothing to cross yet.

Measured consequences (bench/sch, before -> after; the eight designs not listed are unchanged except for the notes):
rp2040-min labels 60 -> 65, `wired_local_ratio` 0.72 -> 0.51, score v2 61.2 -> 47.1, paper still A3 with the frames
in two rows (`sheet_fill` 0.47 -> 0.50), the debug header drawn inside the MCU frame; stm32-sensor wire crossings
0 -> 1 (SWDIO / SWCLK cross once on their way to the header, which is now wired row to row), `wired_local_ratio`
0.68 -> 0.64, score v2 67.3 -> 64.5. The score falls because it counts wired pin pairs; that is the price of the
critics' rules and is NOT compensated. Guards moved in `tests/test_schematic_pro3.py` (`LABELS_MAX`, `WIRED_MIN`),
each with its reason; new test `test_fix3_no_perimeter_wire_no_frame_border_name_no_boilerplate`.

Still open after this round (seen on the sheets, not fixed):

- supply / ground symbols reached by an arch (TP4056 TEMP / EP / VCC, USB-C B12 / A9, ESP32 GND pins 1 / 2, RP2040
  TESTEN) and the two sideways symbols of stm32-sensor (VDDA / VSSA between wired pins): no crossing-free upright
  position exists there, and the order "symbols before stems" hides the wires a square crossing would need;
- no-connect crosses on the pin numbers of short pins, "A1B12" on the generated USB-C symbol, DW01A / LM386 pin
  names, the stray-looking crosses on DW01A / MT3608: library symbols, which G0 / the owner rule do not let change;
- decoupling caps above the part instead of at their pins (`decap_dist_mean_mm` unchanged), the detached cap rows of
  rp2040-min;
- the one crossing at a 4-pin crystal (its case pins are diagonal to its signal pins), BOOTSEL / RUN reaching their
  switches with two or three bends;
- label-only islands (breathalyzer R2, rs485-node R4 / R5 and J2, motor-driver D1), the buck feedback of
  motor-driver drawn as a supply symbol, the LM4040 drawn horizontally, the LM386 gain network;
- sheet composition: rp2040-min and phone-breathalyzer on A3, audio-amp and ne555-blinker small on A4, the half
  empty ATtiny frame of motor-driver, cross-frame wires (ADC_IN, WIPER, VBAT) kept as wires.

## 18. Fix round 4 (2026-11-11): what changed after the fourth critic round

Critics: 6.9 / 10 (netlists identical). Root causes taken this round, by impact:

1. **Page composition** (`page.py`).
   - Only a block that IS a connector is tied to an outer column. A block that merely holds one (the debug header at
     its MCU) may stand in the middle; that connector is then undocked into a frame of its own at the sheet's edge
     (`info["undock"]`, which existed but could not fire because the 3.0 penalty kept the whole MCU on the edge).
     rp2040-min: three rank columns across the sheet instead of the MCU under a row of small frames.
   - `RANK_W` 0.2 -> 0.4 (one rank per column weighs more), a column that holds one pinned connector frame counts
     neither as "stretched" nor as "short", and two new terms (`COVER_W`): the sheet area the arrangement's outline
     leaves empty, and the difference between the tallest and the shortest column. phone-breathalyzer: two columns of
     two frames (outline 0.54 of the sheet) instead of a thin band (0.44).
   - An outer column of ONE connector frame that is wired to a frame of the neighbouring column stands level with
     that frame (audio-amp: the speaker terminal beside the amplifier's output, wired straight across the channel).
     Every other column keeps the common top line.
   - The drawing stands up to `RISE` (0.04 of the sheet height) above the middle: the title block and the notes fill
     the bottom of the sheet, and a drawing centred to the millimetre read as hanging low under an empty band.
2. **Doubled contact of a 4-pin part** (`build._bridges`, `CONTACT_BAR`): pins 1 / 2 of a tactile switch on one
   signal are joined by one bar over the part; the net carries its name once (was: the name on both sides of the
   switch). No bar under the part (`RAIL_PAIR_BAR` stays off), so nothing closes a box around the pins; the router
   still never wraps a wire around a part (`engine.SWITCH_NAMES`).
3. **A connector whose one signal is already taken at the host's pin** (`dock.dock_anchor`, `TAP`): a branch leaves
   the pin's stub two to six rows up or down and gives the connector an open end of its own; the connector is docked
   on it like any other (ne555-blinker J2: wired, 6 names for 6 named nets). A branch on which the connector cannot
   be docked is taken back.
4. **Sheet notes** (`engine.layout_pro`): when the strip under the frames has no room, the NOTES block goes to a free
   spot of the sheet (under a short frame, right of a frame) before it goes into a frame it does not belong to
   (rs485-node: under the sensor frame, not inside the MCU frame).

Tried and taken back: `ARCH_LAST = True` (a square crossing before an arch to a supply symbol), also with shorter
lanes for waiting pins. It removes the hooks at the breathalyzer's USB-C receptacle but costs crossings on three
sheets, a sideways ground symbol on li-ion-powerbank, and with the shorter lanes li-ion-powerbank fell back to the
`pro` rung. The hooks stay.

Not done (still open after this round):

- "candy-cane" hooks to supply / ground symbols at side pins (USB-C receptacles, TP4056, ESP32 module): needs the
  supply pins of one side planned together instead of one after the other;
- decoupling caps that find no room at their pins (stm32-sensor C3, ne555 C1, li-ion C1, three rows on rp2040-min),
  sideways VDDA / VSSA symbols on stm32-sensor;
- label-only pairs between neighbours where the pin orders do not agree (flash on the RP2040: `BUS_WHOLE`; ISP
  header and RS-485 terminal on rs485-node; I2C pull-up island), the termination resistor drawn in line;
- no-connect crosses on hidden `no_connect` pins (DW01A, MT3608, LM4040): the identity gate (`gate.nc_pins`) keeps every flag of the source;
- PWR_FLAG count (kept by the gate) and their side wires; six ground symbols on the ESP32 module (the 18-junction
  comb they replaced was a defect of an earlier round);
- CC pull-down return across D+ / D-, crystal with diagonal case pins, LM386 gain network, LM4040 horizontal, buck
  feedback as a supply symbol, FS8205A left of the DW01A;
- A3 for rp2040-min and phone-breathalyzer (the MCU frames do not fit A4), small drawings on A4 for audio-amp and
  ne555-blinker; raw catalogue strings as connector values (G1).

## 19. State after round 5 (2026-11-11): `pro2` is the default of `sch_tidy`

**Shipped.** `sch_tidy()` without a `style` draws `pro2` (`tool.DEFAULT_STYLE`). `style='pro'` and `style='labels'`
stay selectable. The ladder is unchanged: `pro2` (+ the symfix candidate when `symfix='auto'`) -> `pro2-safe` ->
`pro` -> `pro-plain` -> `labels`, at most three candidates gated, every one through the same identity gate (section
10). The result always carries `style_used`; when that is not the style asked for it also carries `fallback`, one
sentence with the reason the better rungs were refused ("asked for pro2, drawn as pro: pro2 refused (...)"), repeated
in the summary line, and `fallbacks` (each refused rung). The tool's listed description was cut to 867 characters; the
drawing rules, the ladder and the symfix levels are in its `help=` text (`copper_help(tool='sch_tidy')`). `engine.relayout` itself still defaults to `pro`: the
re-layout `sch_apply` does on its own after a position-free batch (`tool.after_apply`) was NOT switched in this step.

**Implemented in `pro2` (what a sheet gets today).** Idiom templates (LDO / boost / buck line, op-amp loop, crystal,
LED chain, divider, USB-C CC, passive idioms) with per-idiom fallback; wires inside a block, names only where a net
leaves it; flow-ranked frame columns, connectors at the outer columns (undocked into a frame of their own when their
host stands in the middle), frames re-fitted to the measured drawings and the smallest paper they fit on (`_refit`,
both second drawings compared, no cross-frame wire turned into names); supplies up / grounds down with PWR_FLAGs on
the net's own wire or open end; one bar over the doubled contact of a 4-pin switch; a two-pin part across two
neighbouring pins drawn across the pair; Reference / Value seated as a pair, inside their frame, off net names;
leads for no-connect crosses that would sit on a pin number; generated notes at the part they explain and the rails
line in the title block; symfix Level 1 hints for every library, Level 2 redraw only for `copper-lcsc` symbols
(owner rule "default symbols first": a KiCad stock symbol is never replaced or redrawn).

**Measured (this tree, KiCad 10.0.6).**

- `bench/sch/run.py` (engine, `symfix=auto`): 10/10 netlist identical, 10/10 no new ERC, 10/10 `style_used == pro2`,
  no fallback on any design.
- `bench/sch/tool_e2e.py` (the TOOL through `registry.call_tool`, tool defaults, independent kicad-cli witness before
  and after): the same 10 designs plus a copy of a user project (phone-breathalyzer from outside the repo): 11/11
  netlist identical, 11/11 no new ERC, 11/11 `pro2`, 4.5 - 11.4 s per design.
- Score v2 before -> after (mean after 74.8; v1 in brackets): c3-devboard 20.3 -> 59.8 (73.2), li-ion-powerbank
  24.3 -> 78.4 (81.2), motor-driver 26.2 -> 76.8 (89.9), opamp-afe 22.9 -> 90.3 (93.3), phone-breathalyzer
  33.6 -> 80.7 (85.7), rp2040-min 22.9 -> 47.4 (67.3), stm32-sensor 28.6 -> 78.0 (86.1); unseen: audio-amp
  13.6 -> 77.1 (92.6), ne555-blinker 30.4 -> 84.6 (97.9), rs485-node 27.5 -> 75.3 (86.2).
- Hard rule violations left: `power_sideways=1` on c3-devboard, li-ion-powerbank and rp2040-min; none on the other
  seven. Wire crossings: 6 / 6 / 0 / 0 / 2 / 6 / 2 (corpus), 0 / 0 / 0 (unseen).
- Independent critics: 7.1 (`bench/sch/critique/round4_defects.md`) -> 7.20 (latest round). Target 8.5: NOT reached.

**Known remaining defects** (from `bench/sch/critique/round4_defects.md` and the pictures of this run):

- sheet use: phone-breathalyzer and rp2040-min on A3 with the lower third empty (`sheet_fill` 0.42 / 0.43); audio-amp
  (0.22) and ne555-blinker (0.33) are small drawings on their paper;
- rp2040-min is the weakest sheet (v2 47.4): 63 names, the QSPI flash and the GPIO header are name-only, three
  detached decoupling rows, a crossing at the 4-pin crystal;
- hooks to supply / ground symbols at side pins and ground symbols drawn above a part (USB-C receptacles, TP4056,
  ESP32 module); one sideways supply symbol on three sheets;
- stacked or crowded pin numbers of stock symbols (USB-C `A1B12`, B-row numbers at the body edge, LM386 pin names,
  DW01A): library symbols, which the owner rule and gate G0 do not let change - reported as symfix hints only;
- name-only islands where a short wire belongs (I2C pull-ups on rs485-node, ALC_SENSE divider on the breathalyzer),
  LED-chain and other fully wired two-pin nets that still carry a name;
- stm32-sensor: the crystal's wires share the channel with the VDDA / VSSA leads; debug / ISP headers inside the MCU
  frame instead of at the sheet edge (stm32-sensor, rs485-node);
- PWR_FLAG count and no-connect crosses on hidden pins are kept by the gate; raw catalogue strings as connector values;
- a caller's block `note` hint directly under the frame title is counted as `text_collisions=1` (`hard`) although
  the two lines do not overprint;
- `sch_apply`'s own re-layout still draws `pro`: after `sch_tidy` (pro2) the next position-free batch redraws the
  sheet in `pro`, and the next `sch_tidy` may draw `pro2` again. Switching `after_apply` needs its own timing and
  token-budget pass.

## 20. Class L (2026-11-11): label-only links inside one block - planned bundles

**Root cause (confirmed in the code).** `dock.dock_anchor` knew ONE wiring form between a main IC and a peer of its
block: straight wires, row on row (`dock_rows`, one common vertical offset). A peer whose pin order differs from the
host order (ISP / SWD / SPI headers) or whose bus pins sit on both sides (a serial flash) could wire a subset at
most; `BUS_WHOLE` then un-wired that subset, and the router was forbidden to repair it afterwards
(`route.TIGHT_MAX_BENDS = 1`, `TIGHT_NO_CROSS`, `WRAP`: it routes one net at a time, so its doglegs were ruled out
one by one). Second cause, order of operations: `Blk.place` gives the main IC's own parts (a pull-up on RESET, the
boot switch on the flash's chip select) the room right in front of its pins before any peer is considered.

**What changed.**

- `sch/pro/bundle.py` (new): the nets two parts share are planned as ONE bundle - two-sided channel routing with a
  free peer pose and offset. Every net runs host end -> its row -> one vertical track -> the row of its peer pin;
  a pin on the far side of the peer is led around the peer's nearer end on nested lanes (at most `MAX_FAR` per
  end) and enters the channel on a wrap row. Track order: a net whose host row is another net's peer row turns
  nearer to the host; among the legal orders the one with the fewest crossings (`best_order`, floor =
  `floor_crossings`: pin pairs whose order differs between the two parts - no drawing can go below it). Tracks
  stand beside the peer or right behind the names at the host (`at_host`). Every crossing is square and 2.54 mm
  or more from a wire end; a bundle wire may cross a wire that is already there (the lead of the peer's own supply
  pin) the same way, and a supply / ground symbol of the peer that stands on a bundle row is moved along its lead
  (`clear_rows`). All nets of the pair are wired or none (as before); a bundle that needs more than
  `max_cross(n)` = n - 1 crossings stays two name columns (`CROSS_PER_NET = 0.5` halves the allowance).
- `dock.dock_anchor`: when not every shared net can run straight, `bundle.find` is asked - for the pose the old
  rule picked and, when that needs a crossing, for the other poses that keep supply pins upright; the plan with the
  fewest crossings / covered host rows / bends is drawn (`bundle.commit`). The old path (rows, `BUS_WHOLE`, attach)
  is the fallback, unchanged.
- `dock.build_block`: a block whose peers are still joined by names is drawn again with the parts on the shared
  nets deferred (`Blk.no_stem`) and, if that is not enough, with the shared ROWS kept free while the IC's other
  parts are placed (`Blk.row_lanes`; a stage idiom - the crystal - is tried with these lanes first and without them
  second). The drawing with the lowest `block_cost` (missing wires + 3 per lost stage idiom) is kept; a draw keeps
  the plain one. The choice is remembered per block (`_CHOICE`), the engine draws a sheet several times.
  `RESERVE_PHANTOM` (the peer's whole place reserved from a scratch drawing on the bare IC) is implemented and off:
  on the bench it never beat the other two.
- The names of a bundle stand in one column at the host's pins (`Cell.name_at`, `engine._name_on_wire(prefer=)`).
- Metric (`score.measure`, `bench/sch/run.py` acceptance, `bench/sch/links.py` to compare two runs):
  `label_only_links` = per net and frame, wire groups - 1; `label_only_pairs` = part pairs of one frame that share
  signal nets with no wire at all between them; `label_only_link_list` names them.

**Measured (bench/sch, 10 designs, before -> after).** Netlist identical 10/10, no new ERC, `pro2` 10/10, hard
violations unchanged. `label_only_links` 29 -> 23; without the seven links between two pins of ONE part (the far side
of a pass-through TVS array, the two drains of a dual FET: names by the critics' rule of fix round 2) 22 -> 16:
rp2040-min 8 -> 4 (QSPI flash wired: 6 nets, 5 crossings = the floor of its pin order; the boot switch hangs on a
branch of the chip-select wire; the debug header now stands inside the MCU frame and its two names are counted
here - before, it stood in a frame of its own and the same two names were cross-frame), stm32-sensor 2 -> 1 (SWDIO /
SWCLK both wired, one crossing = floor), li-ion-powerbank 2 -> 1 (BATN between the protection IC and the dual FET).
Labels 236 -> 228. Wire crossings 22 -> 28 (rp2040-min 6 -> 11, stm32-sensor 2 -> 3), every added one square and
2.54 mm or more from a wire end. Time per sheet: rp2040-min 11 s -> 18 s, stm32-sensor 9 s -> 12 s, rs485-node
8 s -> 13 s (a block with peers is drawn up to three times; the choice is remembered for the later drawings).

**Not reached, and why.** The target was -70 % with no added crossing. The two contradict each other: a wire between
two parts whose pin orders differ crosses once per inverted pair, whatever the drawing. What is left:

- motor-driver J4 (4 links) and opamp-afe J3 (3): the header's pins are +5V, GND, then the signals. The ground lead
  would have to cross every signal wire (4 / 3 crossings of a supply wire over signals right at the pins, plus 3 / 2
  for the pin order): 7 and 5 crossings for 4 and 3 nets. They stay name columns by `max_cross`.
- rs485-node (6): the ISP header needs the rows of MOSI / MISO / SCK and RESET; the LED chain's ground symbol stands
  on the MOSI row and the crystal's ground on the RESET row. With the rows reserved the header is wired and both
  I2C pull-ups stand at their pins, but the crystal loses its place at the pins and becomes an island (two names):
  `block_cost` calls that a draw and keeps the plain drawing. Needs the crystal idiom of class I (a drawing between
  its two rows) or an LED chain whose ground symbol can stand elsewhere.
- stm32-sensor NRST (the header pin is on the other side of the MCU than NRST); rp2040-min's debug header (2): the
  crystal stands in front of the SWCLK / SWDIO rows, and a stage idiom keeps its place at the pins.
- Tracks of a bundle are one global zone (beside the peer or behind the names): a net whose host end lies far out
  (the end of a stem) cannot turn earlier than the others.

### 20.1 Second pass of class L (2026-11-11)

**Acceptance number.** `score.label_only_part_links` (also in `bench/sch/run.py` and `bench/sch/links.py`): label-only
links between DIFFERENT parts of one frame. A second pin of ONE part on the net (the far side of a pass-through TVS
array, the second drain of a dual FET: 7 of the 29 `label_only_links` of the baseline) is no link between two parts and
no placement changes it; it stays in `label_only_links`. Baseline 22 (`bench/sch/out/_L_base`, `run.L_BEFORE`).

**What changed in this pass.**

- **Island pull-ups go to the other pin of their net** (`engine._rehome`, switch `REHOME`; `dock.islands`). A pull-up /
  pull-down / filter cap that finds no room at the pins of its own IC and would stand beside it with a name is drawn
  in another block that holds a pin of the same net, on the wire of that pin - when that block takes it without
  losing anything (`block_cost`, no new island) and the source block gets better. The move is written into the design
  (`BlockInfo.sats`, `Design.block_of`, the part's role anchor: its note moves with it) and reported
  (`idioms.rehomed`). rs485-node: the I2C pull-ups stand at the LM75B's SDA / SCL pins (the crystal hangs over those
  rows of the ATmega). `score` measures a pull-up's distance to the nearest IC pin of its NET.
- **A supply / ground lead of the peer is led over the tracks** (`bundle.tuck_leads`, switch `TUCK`). A 1x6 header
  with +5V, GND on pins 1, 2 had its ground symbol on the rows of the signal pins; `clear_rows` could only run the
  lead on through those rows (one crossing per wire, "7 crossings for 4 nets"). Now the lead runs along its own pin
  row past the last track and turns to its symbol there; the wires of the rows below turn before they reach it, and
  when they turn away from it nothing is crossed. Both forms are planned (`find`), the better plan is kept. A row of
  air is kept beyond the symbol, so the name of the wire that passes there stands in the name column.
  opamp-afe: SPI header wired (3 wires, 2 crossings = the floor of its pin order).
- **A wired connector is not undocked** (`page.KEEP_WIRED`, `Blk.wired`): a header joined to its IC by two or more
  wires stays in the IC's frame when that frame stands in a middle column (taken to the sheet edge it became a column
  of names again).
- **A bundle may not cost a paper size** (`engine._paper_guard`, switches `PAPER_GUARD`, `GUARD_FROM`). When the
  sheet with bundles needs a paper beyond A4, it is drawn once more without them (one probe drawing with estimated
  and one with measured sizes; the full second pipeline only when the probe fits a smaller paper), and that drawing
  is taken when its paper is smaller (`page.paper_guard` says so). motor-driver: the wired programming header
  (4 wires, 3 crossings) makes the MCU frame 2.54 mm taller than the column over the title block of an A4 sheet has;
  the sheet stays A4 with the header as a name column. The wired drawing is kept by the test with the guard off.
- **A peer on one net of a top / bottom pin is docked in line with the pin** (`dock.PEER_IN_LINE`): the reference of
  opamp-afe on the converter's VREF pin is joined by one straight wire (its stub runs on until the line is clear of
  its parts' texts). It used to be dropped by the slide search and wired by the router or not, as its place allowed.
- **Only the nets of the peers that are left with names wait** (`dock.build_block`): the deferred / lanes drawings
  no longer hold back the parts on the net of a peer that is wired as it stands.
- **A deferred part is never tapped back past the pins it comes from** (`dock.tap_deferred`): a pull-up at the end
  of a wire around its IC is no drawing of a pull-up.
- **A signal edge inside one column costs the page** (`page.SAME_COL_W` = 0.3 per signal edge between two frames
  of one column that are not pinned connectors): with the reference docked over its pin the ADC frame of opamp-afe
  got narrower and the composer stacked the ADC over the amplifier - ADC_IN, a wire across two frames since fix
  round 6, became a pair of names. With the term the amplifier stays left of the ADC (`cross_block_wires` 1,
  `sheet_fill` 0.50 -> 0.56); no other bench sheet changes its arrangement.
- `bundle.find` remembers its answer per scene (`_FIND_MEMO`: the engine draws a sheet several times; rp2040-min
  25 s -> 20 s without the paper guard's extra drawings). `engine._refit` draws once more when a strip that was kept
  under a drawing for its notes is no longer used (rp2040-min: 9.4 mm of air under the MCU drawing).

**Measured (bench/sch, 10 designs; `_L_base` -> first pass -> this pass).** Netlist identical 10/10, no new ERC,
`pro2` 10/10, hard violations unchanged, papers unchanged.

| | `label_only_part_links` | `label_only_links` | wire crossings | labels |
|---|---|---|---|---|
| li-ion-powerbank | 1 -> 0 -> 0 | 2 -> 1 -> 1 | 6 -> 6 -> 6 | 23 -> 22 -> 22 |
| motor-driver | 4 -> 4 -> 4 | 4 -> 4 -> 4 | 0 -> 0 -> 0 | 27 -> 27 -> 27 |
| opamp-afe | 3 -> 3 -> 0 | 3 -> 3 -> 0 | 0 -> 0 -> 2 | 14 -> 14 -> 11 |
| rp2040-min | 6 -> 0 -> 0 | 8 -> 2 -> 2 | 6 -> 11 -> 11 | 63 -> 57 -> 57 |
| stm32-sensor | 2 -> 1 -> 1 | 2 -> 1 -> 1 | 2 -> 3 -> 3 | 16 -> 15 -> 15 |
| rs485-node | 6 -> 6 -> 4 | 6 -> 6 -> 4 | 0 -> 0 -> 0 | 29 -> 29 -> 27 |
| the other four | 0 | 4 (TVS arrays) | unchanged | unchanged |
| total | 22 -> 14 -> 9 (-59 %) | 29 -> 21 -> 16 | 22 -> 28 -> 30 | 236 -> 228 -> 223 |

**Not reached, and why.** The target was -70 % with no added crossing.

- Crossings: every added one is the floor of a pin order (QSPI flash 5, SWD header 1, SPI header 2): a wire between
  two parts whose pin orders differ crosses once per inverted pair, whatever the drawing. "Wired" and "no crossing
  added" exclude each other for these parts.
- motor-driver J4 (4 links): the wired drawing exists and is tidy, and is refused by the paper guard (above). It
  comes back by itself when the page takes the frame (a composer that does not stack the MCU over the title-block
  column, a smaller border: class P).
- rs485-node J3 (4): the RESET row of the ATmega lies under the crystal's ground symbol and the crystal's caps
  stand in front of the rows behind the names; MOSI / MISO / SCK alone would be three wires with three crossings
  (two for the pin order, one of the header's +5V lead over MISO), more than `max_cross`. Needs the crystal idiom of
  class I (a drawing between its two rows).
- stm32-sensor NRST (1): the header pin is on the other side of the MCU than NRST.
- The debug header of rp2040-min stands in a frame of its own (two names across frames; not counted above): the
  crystal stands in front of the SWCLK / SWDIO rows.
- Time: a block with peers is drawn up to three times and the paper guard adds up to two probe drawings on sheets
  larger than A4: rp2040-min 26 s, motor-driver 20 s, stm32-sensor 15 s, rs485-node 16 s per sheet on this machine
  (shared with other benchmarks); they were 11 / 7.5 / 9 / 8 s before class L.
