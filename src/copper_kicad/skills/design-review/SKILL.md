---
name: design-review
description: Expert engineering review of a KiCad schematic and PCB with Copper before ordering — power tree, decoupling, USB-C, ESD, crystals, resets/straps, pull-ups, thermal, DFM against the fab profile's rules (JLCPCB by default). Use when the user asks to "review", "check", "sanity check" or "is this ready to order" for a board, or after finishing a design and before fab output.
---

# Design review with Copper

ERC and DRC show that the design is *self-consistent*. A review checks whether it will *work*. Run the
automated passes first, then work through the checklist yourself, reading datasheets where needed.

## 1. Automated passes

1. `verify(what=["erc","drc"])` → every error must be resolved or explicitly justified.
2. `design_review` → heuristic findings (missing decoupling, CC resistors, crystal load caps, pull-ups, ESD) and
   the `schematic_style` pass (drawing rules, each finding names its rule in `section`).
   Each finding has a `fix`. Treat `high` findings as blockers.
3. `parts_source_bom(write=false)` → 100% of lines are sourced (an LCSC number with stock in the default setup; the
   project's part-number field with other parts providers).
4. `render(target="schematic")` and `render(target="pcb")` → **look** at both.

## 2. Schematic checklist

**Power tree**
- List every rail: source, regulator, max load, regulator dissipation P = (Vin − Vout) × I.
  A SOT-223 LDO in still air handles ≈ 1 W and a SOT-23 ≈ 0.3 W.
- Input protection: reverse polarity (P-FET or Schottky), TVS on external supply inputs, fuse/PTC where a short
  could burn a trace.
- Divider-set regulators: compute Vout = Vref × (1 + R1/R2) using the datasheet's Vref and check it against the
  rail name.
- Enable pins tied to a defined level, never floating.

**Decoupling**
- 100 nF at every supply pin plus bulk per rail. MCU analog supply filtered.
- Regulator in/out caps match the datasheet's stability requirements (ESR range, minimum µF).

**Interfaces**
- USB-C sink: 5.1 kΩ on CC1 and CC2 *individually*; both A6/B6 (D+) and A7/B7 (D−) joined; shield to GND
  (directly or via 1 MΩ ∥ 4.7 nF); ESD array on D+/D−; VBUS TVS.
- I²C: exactly one set of pull-ups per bus, to the bus voltage.
- UART: TX↔RX crossed between devices.
- SPI/QSPI flash: CS pull-up so the device stays deselected during boot.
- Level compatibility: never drive a 3.3 V-only input from 5 V logic.

**MCU**
- Strapping/boot pins at the correct level for normal boot (check the strapping table). BOOT and RESET buttons
  reachable.
- Crystal: C = 2·(CL − Cstray), C0G dielectric; check whether the oscillator needs an external Rf/Rs.
- Programming/debug header (SWD, UART or native USB) actually connected.
- Unused pins: no-connect flags. No floating inputs on CMOS logic.

**Indicators and misc**
- LED current within the GPIO limit. Power LED current counted in the low-power budget.
- Test points on every rail and on key signals.

**Drawing style** (rules and ids in `docs/SCHEMATIC_STYLE.md`; the `schematic_style` pass measures the first group)
- Measured: supplies up, grounds and negative rails down; no four-way junctions; no label hops between neighbours;
  no bare `VCC`/`VDD` rail names; rails with a min / typ / max voltage and current note; passives with value,
  tolerance, rating and package or a standard-parts note on the sheet; inter-sheet references on; discrete
  transistors with their envelope circle; no package-shaped or unfilled IC symbols; test points with an expected
  reading; floating inputs explained; root sheet a block diagram; no crammed sheet.
- By eye, on the render: can each block be recognised in a blink? Flow left to right (a switcher's feedback pin on
  the right is fine)? Groups separated by whitespace and titled? Direction arrows on bidirectional and RF paths?
  Sense wires at 45° at the sense point? Circuit common, chassis and earth kept apart? Anything intentional but
  odd called out in a note?
- MOSFETs oriented correctly for their body diode; the design fails safe.

## 3. PCB checklist

- Decoupling caps within 2 mm of their pins, with a short via to GND.
- Crystal within 5 mm, no signals routed under it, guard ground.
- USB D+/D− as a 90 Ω differential pair, length-matched within ~1 mm, no stubs, continuous ground beneath.
- Switching regulators: smallest hot loop (input cap ↔ switch ↔ diode/FET), feedback trace away from the
  inductor.
- Current-carrying traces sized for current (0.3 mm ≈ 1 A on 1 oz outer copper). Thermal pads with via arrays.
- GND pour on both sides, stitched. No isolated copper islands. Zones refilled (`verify` refills them).
- Connectors on board edges, with courtyard clearance from the enclosure. Mounting holes clear of copper (or tied
  to GND intentionally).
- Silkscreen: refdes readable and not on pads; pin-1 markers; polarity marks on diodes, LEDs and electrolytics.
- DFM against the active fab profile (`fab_export(profile='list')`); for the default jlcpcb profile: trace/space ≥ 0.127 mm (design at 0.2), via ≥ 0.3 mm drill / 0.6 mm pad, copper-to-edge ≥ 0.3 mm,
  annular ring ≥ 0.13 mm, hole-to-hole ≥ 0.5 mm.

## 4. Report format

Group findings as **Blockers / Should fix / Nice to have**. Give each one a location (ref, net or x,y) and the
exact Copper call or edit that fixes it. End with a verdict: *Ready to order* or *Not yet (N blockers)*.
