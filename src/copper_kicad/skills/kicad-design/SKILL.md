---
name: kicad-design
description: End-to-end KiCad board design with the Copper MCP tools — from a one-line requirement to a verified, orderable board (JLCPCB by default; any fab through fab profiles). Use whenever the user asks to design, draw, build, lay out or modify a schematic or PCB in KiCad (e.g. "make a USB-C ESP32-C3 board", "add a status LED", "route this board"). Covers requirements, part selection from the project's parts providers (LCSC/JLC Basic parts by default), batched sch_apply edits, ERC/DRC verification, rendering, PCB setup/placement/routing and fab output.
---

# Designing a KiCad board with Copper

Copper gives you KiCad as tools. KiCad itself (kicad-cli) checks every change,
so trust the ERC/DRC counts in each result, never your own assumptions. The loop is always:

**plan → whole schematic in one `sch_apply` batch → fix what its ERC reports → `board_build` → fix what its report lists → fab pack**

Every call re-reads the whole conversation, so fewer, larger calls are cheaper. Results already carry KiCad's ERC/DRC
counts, a `fix` per finding and `next`: do not call `verify` or `render` after each step.

## 0. Orient (1 call)

`copper_status` — KiCad version, active project, tool health. If there is no
project, `project_create(name=...)` (the fab profile's rules and a 1.6 mm
stackup preloaded; `fab="<profile>"` picks another fab) or `project_open(path=...)`. `copper_status` shows the
active parts providers and fab profile under `config`.

## 1. Requirements → block list (think, no tools)

Write a short spec before touching the schematic:

- Supply: source (USB-C 5 V, Li-ion 3.0–4.2 V, 12 V barrel…), rails needed, worst-case current per rail.
- Blocks: input protection → regulation → MCU/main IC → peripherals → connectors/indicators.
- Every IC: read its datasheet's *typical application* circuit; that is your schematic.
- Board: size, layer count (2 unless density/RF demands 4), mounting holes, connector edges.

## 2. Parts — from the project's parts providers

The default provider is `lcsc` (LCSC parts for JLCPCB assembly, JLC Basic preferred), described below. When
`copper_status` shows other providers (`local` catalogue, `kicad` generic parts, third-party), the same calls work:
results name their `provider`, and the part-number field is the provider's own instead of `LCSC`.

- `parts_search(query="10k 0402", basic_only=true)` → pick the Basic part with stock. Basic parts have no
  JLC setup fee; each Extended part adds ~$3 per unique part to assembly.
- Passives: one package size for the whole board (0402 for density, 0603 for hand-rework). Use E12/E24 values
  that exist as Basic parts — 10k, 4.7k, 5.1k, 1k, 100k, 22R, 100nF, 1µF, 10µF, 22pF are all common Basic parts.
- ICs: prefer parts with ≥ 1,000 JLC stock and a KiCad stock symbol; `parts_import(lcsc="C…")` brings symbol,
  footprint and 3D model for anything else into the project's `copper-lcsc` library and stamps the LCSC field.
- With the default setup every placed part must end up with an `LCSC` field — `fab_export` (profile `jlcpcb`) refuses
  unsourced BOM lines. Parts also carry `MPN` + `Manufacturer` whenever known, whatever the provider.

## 3. Schematic — `sch_apply` with big batches

One call per functional block (10–60 ops). Place, then connect pins **by name**. Never compute wire coordinates.

**Wires first, labels last.** A reader sees a wire in a blink and has to search for a label. Join the parts of one
block with `wire` ops pin to pin (`{"op":"wire","from":"R1.2","to":"U1.EN"}`); Copper routes the wire. Use `connect`
with a net name for power and ground (they become power symbols), for nets that leave the sheet or the block, and
for runs that would be awkward to draw. Never join two neighbouring parts with a pair of labels. A wired net that
matters still gets its name (`label` op) so it can be found and cross-checked. On a sheet only Copper has drawn,
`sch_tidy` turns the remaining in-block label pairs into wires.

```json
{"ops": [
  {"op": "place", "lcsc": "C165948", "ref": "J1", "group": "connector"},
  {"op": "place", "spec": "5.1k 0402", "ref": "R1", "near": "J1"},
  {"op": "place", "spec": "5.1k 0402", "ref": "R2", "near": "J1"},
  {"op": "connect", "pins": ["J1.A4", "J1.B4", "J1.A9", "J1.B9"], "net": "VUSB"},
  {"op": "wire", "from": "J1.A5", "to": "R1.1"}, {"op": "label", "net": "CC1", "pin": "R1.1"},
  {"op": "wire", "from": "J1.B5", "to": "R2.1"}, {"op": "label", "net": "CC2", "pin": "R2.1"},
  {"op": "connect", "pins": ["R1.2", "R2.2", "J1.A1", "J1.B12", "J1.S1"], "net": "GND"},
  {"op": "no_connect", "pins": ["J1.A8", "J1.B8"]},
  {"op": "text", "text": "VUSB: 4.5 / 5.0 / 5.5 V, 500 mA max (USB 2.0 default)"},
  {"op": "text", "text": "J1.A8, J1.B8 (SBU): intentionally left open, no alt mode"}
]}
```

- Give new parts temporary `id`s and use them in pins (`"pin": "led.A"`); the result maps each id to its final ref.
  A pin *name* that matches several pins (`J1.VBUS`, `U1.GND`) connects all of them.
- `sch_read` shows what is already on the sheet (refs, pins, nets) before you edit an existing design.
- Every unused IC pin gets `no_connect`; every power input pin must be driven (a `PWR_FLAG` on rails that only
  come from a connector, otherwise ERC reports "power input not driven").
- `dry_run=true` validates a batch without writing.

## 4. Read the ERC in each result

Every `sch_apply` result has `verify.erc`: counts plus findings, each with a `fix` hint naming the call that resolves it
(a separate `verify` call is only needed after edits made outside Copper). Fix all
errors before moving on. Treat these warnings as errors: two outputs on one net, labels that merge different
nets, pins of a power IC left unconnected.

## 5. Look at it

`render(target="schematic")` once the schematic is complete (pictures are only attached on request) and hold it
against the style rules below. `sch_tidy` re-lays out a messy sheet. `design_review(checks=["schematic_style"])`
measures the rules that can be measured and gives a `fix` for each.

## 5a. Schematic style: draw it so a stranger reads it in a blink

Other people will read this schematic. These rules are not optional and they override any older habit, including
older examples in tool help. Follow each one until it plainly makes the sheet worse, then break it and leave a note
saying why. Full list with rule ids: `docs/SCHEMATIC_STYLE.md`.

**Flow**
- Signal and power flow left to right: inputs left, outputs right. Enables and other control inputs sit with the
  inputs. A switcher's feedback pin on the right is fine: it is part of the output circuit. If the flow cannot be
  left to right, leave enough whitespace that the eye can follow the path.
- Ground and negative-supply symbols always point down. Positive-supply symbols always point up. Never sideways.
- Where direction is not obvious (bidirectional or RF chains) add an arrow or a note that says which way it flows.
- Draw standard circuits in their textbook shape (divider, RC filter, follower, inverter, LDO, crystal) so they are
  recognised, not decoded.

**Symbols**
- Functional symbols, never the package pinout: supplies on top, grounds at the bottom, inputs left, outputs right,
  related pins together. The board shows the package; do not paste a pinout picture beside the part. Only RF parts
  whose schematic is their layout are exempt.
- IEEE 315 conventions: every mark means something. A discrete transistor is drawn with its envelope circle.
- Duplicate supply and ground pins are hidden or stacked, unless the design is safety-critical: then show every pin.
- Symbol bodies are filled with the background colour, not transparent.

**Grouping and sheets**
- Whitespace is the separator. Group each function, leave space between groups, and give every group a title.
  A box is only for a sheet too tight for whitespace.
- As few sheets as possible, but never crammed: one sheet per module (power supply, MCU, connectors). Prefer
  11x17 in (A3 class) sheets. Out of whitespace means add a sheet, not shrink the drawing.
- A multi-sheet design has a block diagram on the root sheet, built from hierarchical sheet symbols wired in
  signal-flow order, with no circuitry of its own.

**Connections**
- Wires over labels (section 3). Labels only for off-sheet nets and awkward runs; no label hop between neighbours.
- Global and hierarchical labels are equal to the reader: hierarchical for sheets on the block diagram, global for
  nets that go everywhere. Inter-sheet references stay on (new Copper projects have them on).
- At most three wires meet at a junction. A four-way junction is forbidden: stagger it into two tees.
- A sense (Kelvin) wire leaves the conductor at 45° at the exact point it senses.

**Power**
- Rail names say where the power comes from or what it feeds: `VUSB`, `VBAT`, `+3V3_MCU`. Never a bare `VCC`/`VDD`.
- Beside each rail's source write min / typ / max voltage and typical / maximum current
  ("VUSB: 4.5 / 5.0 / 5.5 V, 35 mA typ, 500 mA max"); at the very least "9-19 V in" or "3V3, 2 A max".
- Power is distributed with power symbols, not ordinary labels.
- Circuit common (0 V / GND), chassis and earth are different things with different symbols. Never the earth symbol
  for circuit common; if a design has more than one reference, a note says what each is.

**Notes** (the `text` op; use it freely)
- Every test point: what to expect there (voltage, waveform, scope setting).
- Everything intentional but odd: "pins 11-13 intentionally left floating", "IN1 and IN2 shorted on purpose".
- Passives (R, C, L, diodes) carry value, tolerance, power or voltage rating and package ("10k 1% 0.1W 0402");
  check the package can take the rating. ICs do not need this.
- Every sheet has a standard-parts note ("Unless noted: resistors 1% 0.063W 0402; capacitors X7R 16V 0402") so
  parts that follow it show only their value.

**Before calling the schematic done**: power through power symbols; every supply pointing the right way; important
nets descriptively named; MOSFETs oriented correctly for their body diode; bypass capacitors on every IC; the design
fails safe; modules grouped separately.

## 6. PCB

`board_build(width=30, height=40, radius=1, holes="M3")` runs steps 1 to 6 below in ONE call and returns one report with
a small picture of the board: use it first (call it again when it returns `pending`), then use the single steps only to
fix what the report lists.

1. `pcb_sync` — footprints + nets from the schematic (keeps existing placement).
2. `pcb_setup(width=30, height=40, radius=1, holes="M3", rules="fab")` — outline, mounting holes, the fab profile's design rules.
3. `pcb_place(auto=true)` — connectors on edges first, then the main IC, then decoupling caps *at their pins* (≤ 2 mm),
   crystal ≤ 5 mm from the MCU, regulators near the input. `render(target="pcb")` and adjust.
4. `pcb_route` — hand-route critical nets first if needed (`mode="tracks"`: USB D+/D-, crystal, switching-regulator
   loop), then `pcb_route()` autoroutes the rest with Freerouting and re-runs DRC.
5. `pcb_pour` — GND on both layers of a 2-layer board, stitching vias near IC ground pins and along edges.
6. `verify(what=["erc","drc"])` — zero unconnected items and zero clearance errors. `render(target="pcb3d")` for a final look.

## 7. Release

Hand off to the **fab-release** skill: `fab_export` → gerbers, drill, BOM and placement file in the fab profile's format.

## Practical EE defaults (use unless the datasheet says otherwise)

| Topic | Default |
|---|---|
| Decoupling | 100 nF X7R per supply pin, at the pin; + 10 µF bulk per rail; analog supply (VDDA) via ferrite bead + 1 µF + 10 nF |
| USB-C sink (device, 5 V only) | **separate** 5.1 kΩ from CC1 and from CC2 to GND (one shared resistor breaks e-marked cables); VBUS TVS; D+/D- ESD array (e.g. USBLC6-2SC6) at the connector; 22–27 Ω series on D+/D- only if the PHY datasheet asks |
| LDO | input 1–10 µF, output per datasheet: AMS1117 needs ≥ 22 µF (ESR-sensitive, 1.1 V dropout — not for Li-ion→3.3 V); ME6211/XC6206/XC6220 are stable with 1 µF ceramic and have ~0.1–0.25 V dropout |
| Crystal | load caps C = 2·(CL − Cstray), Cstray ≈ 3 pF → CL 12 pF ⇒ 18 pF, CL 20 pF ⇒ 33 pF (use 22 pF nearest Basic); guard with GND, no traces under it |
| Reset / enable | 10 kΩ pull-up + 100 nF–1 µF to GND (ESP32 EN: 10 k + 1 µF) |
| I²C | 4.7 kΩ pull-ups at 100 kHz, 2.2 kΩ at 400 kHz, one set per bus |
| LEDs | 1 kΩ from 3.3 V gives ~1 mA (plenty for modern LEDs); 330 Ω for ~4 mA |
| ESD | every connector pin that leaves the box: TVS/ESD diode at the connector, before series parts |
| Boot / strap pins | read the MCU's strapping table; add BOOT button to GND and RESET button, both with pull-ups |

## Rules of thumb for layout (1 oz outer copper; the limits quoted are the default jlcpcb profile's — `fab_export(profile='list')` has each fab's)

- Signal trace 0.2 mm, clearance 0.2 mm (JLC minimum 0.127/0.127 mm — don't design at the limit).
- Current (≈10 °C rise, outer layer): 0.3 mm ≈ 1 A, 0.5 mm ≈ 1.5 A, 0.8 mm ≈ 2 A, 1.5 mm ≈ 3 A.
- Vias 0.6 mm pad / 0.3 mm drill (JLC standard, no surcharge). Two vias per power/GND transition above 1 A.
- Copper ≥ 0.5 mm from board edge (JLC minimum 0.3 mm); silkscreen ≥ 0.15 mm line, ≥ 1 mm text height.
- Keep a solid ground under high-speed and switching nets; never split the ground under USB data lines.

## When tools are not enough

Direct edits to `.kicad_sch` / `.kicad_pcb` text are allowed for anything the tools don't cover. Close the file
in KiCad first (or re-open it afterwards with File → Revert), keep S-expressions balanced, then call `verify`.
