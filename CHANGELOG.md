# Changelog

All notable changes to Copper for KiCad are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Schematic style rules.** Copper now draws and reviews schematics against one written rule set
  ([docs/SCHEMATIC_STYLE.md](docs/SCHEMATIC_STYLE.md)), taken from the KiCon talk on drawing good schematics and
  its Q&A. Where a rule disagrees with an older Copper convention, the rule wins.
- `design_review` has a new `schematic_style` pass (also part of `verify(what=['review'])`). It reads the saved
  sheets and reports power symbols pointing the wrong way, four-way junctions, label hops between neighbouring
  parts, bare `VCC`/`VDD` rail names, rails without a voltage and current note, passives without tolerance,
  rating or package, missing standard-parts notes, inter-sheet references switched off, discrete transistors
  without their envelope circle, package-shaped or unfilled IC symbols, test points without an expected reading,
  unexplained floating inputs, a root sheet that is not a block diagram and crammed sheets. Every finding names
  its rule and carries a `fix`.

### Changed

- The `kicad-design` and `design-review` skills teach wires before labels, descriptive rail names with
  min / typ / max notes, notes for anything intentional but odd, full passive specs and the release checklist.
  The `sch_apply` help says the same.
- New projects have inter-sheet references switched on.

## [1.1.0] — 2026-11-12

What is new since 1.0.0, in short:

- **Router v2 is the default.** `pcb_route_guided` now routes in stages (power first, fine-pitch fan-out,
  corridors, detail search, repair, polish) with KiCad's own DRC as the judge. The old single-pass router is
  still there as `engine: "guided"`.
- **Length matching, differential pairs and width classes.** Length-matched groups get tuned with chamfered
  serpentines; differential pairs are routed as coupled lanes with skew compensation; per-layer widths and
  impedance classes are honoured; keep-short nets stay short. Constraints come from the plan or from KiCad's
  own rules and net classes, and every result reports what was met and what was not.
- **Cleaner schematics.** `sch_tidy` draws in the new `pro2` style by default: standard drawings for common
  circuits (regulators, op-amp loops, crystals, LED chains, dividers), signal-flow columns and design notes.
  Nothing is written unless KiCad's netlist stays identical.
- **KiCad's own symbols first.** Placing an LCSC part uses a stock KiCad symbol whenever it passes a
  pin-by-pin check against the supplier's pin data; otherwise the imported one.
- **Better parts.** `parts_search` finds long-tail parts by description, flags partial matches and lists
  out-of-stock hits separately. `fab_export` checks live stock for every BOM part and spells out JLC
  assembly fees.
- **Board details.** Locked footprints (`lock` / `unlock`), footprint Edge.Cuts graphics moved out of the
  board outline on import, fewer false decoupling warnings in `design_review`.
- **Easier to install.** New npm launcher: `npx -y copper-kicad` runs the server on KiCad's own Python, so any
  MCP client can start Copper without installing Python. The Claude Code / Codex plugin uses it too. The
  one-line installers now install the wheel from the GitHub release (checksum-verified). The project lives at
  `github.com/claynicholson/copper-kicad-mcp`; there is no PyPI package.
- **Setup speaks npx.** Started through `npx -y copper-kicad`, every hint in the terminal and on the setup page
  names `npx -y copper-kicad <command>` instead of a `copper` command that is not on PATH, and
  `npx -y copper-kicad setup --demo` keeps the launcher's own files in a temporary folder too. The autorouter
  download is given as about 120 MB everywhere (Java ~60 MB + Freerouting ~60 MB, measured).
- **Plainer look.** The README leads with the release video (`copper-release.mp4`, a release asset), the
  Windows installer's side panel and header are drawn from the RP2040 Macropad's own `.kicad_pcb` with no
  text, and the quick-start page, icons and share card are light and plain.

### Details

- `fab_export` (JLCPCB): removed a rotation rule that turned the HRO TYPE-C-31-M-12 USB-C receptacle 180° in the
  placement file, so JLC would have placed it facing into the board.

- `pcb_route_guided` / `pcb_route_plan`, **fixes after a second independent verification**:
  - **A board with existing copper routes the same whatever order KiCad saved its footprints in.** The board
    model renumbers KiCad's connectivity clusters and wrote the new numbers back into the board extract it was
    built from. The router builds several models from one extract, and from the second on, existing tracks and
    vias were matched to the wrong cluster or to none. Depending on the file order (KiCad saves by UUID, so it
    differs from build to build of the same design) a locked track then counted as the copper of the pad at its
    far end: the router joined the near pad to it with a useless loop, left the far pad open and reported 23/24
    with an empty `failed`. The extract is no longer altered; the same board in three file orders now routes to
    the same copper, 24/24.
  - **Measuring a routed board never proposes copper for a pad that is connected.** Same cause: on the routed
    4-layer bench board `cons_mixed` a second call saw a pad that hangs on the ground plane as cut off, proposed
    a dog-bone, discarded it and returned no `constraints` block under the summary "0/0 connections - no
    connection could be routed". It now answers "nothing to route" with the same figures as the routing call.
  - **A run that keeps none of its copper reports the board as it is.** When nothing was closed, or a rip-up
    bought nothing and was undone, the result used to drop the `constraints` block while `diff_pairs` still
    pointed at it. It now carries the block of the unchanged board (measured on the file; no KiCad verdict is
    claimed for it), and with nothing open to begin with the summary says so instead of "no connection could
    be routed".
  - **No silent loss when KiCad and the router disagree.** A net KiCad's ratsnest still shows open although the
    router had every connection of it laid is now listed in `failed` with the pads KiCad holds apart.

- `pcb_route_guided` (engine "v2"), electrical constraints, **fixes after an independent verification**:
  - **A length-group member boxed in by its neighbours is no longer left short on a board with room elsewhere.**
    When every meander amplitude on the net's own layers leaves it short, a stretch of one of its straight runs
    moves to another layer the net may use, through two vias: the run itself with its bumps, or on a short run
    two vias 1 mm apart with one loop between them. The vias' height counts as length (as in KiCad). Vias keep
    the clearance from the net's own pads and stay out of forbidden areas; `nets[..].max_vias: 0` (or a KiCad
    `via_count` rule, or `regions[].no_vias`) keeps a net on its layer; at most two such stretches per net. The
    result says it: `constraints.groups[].hops` and a note naming the nets and layers. If KiCad then reports an
    open connection or an error nobody can be blamed for, these stretches are taken out first. The bench board
    `cons_mixed` (QSPI group, 0.3 mm) now meets every rule by KiCad's own DRC; an explicit `groups[].len` a
    little above the natural length is met on `cons_bus_match`.
  - **A skew limit that cannot be met never leaves the pair worse than a looser one.** With `diff_pairs[].skew:
    0.005` every skew bump was taken out again and the pair ended at 0.39 mm (a limit of 0.15 gave 0.05 mm);
    now it ends at 0.003 mm. Lower bumps are tried where a high one does not fit.
  - **Per-layer widths reach every layer.** With `nets[..].widths` on some layers, pad escapes on an unlisted
    layer were laid at the widest listed width; they, and copper laid by the fallback search, are now at their
    own layer's width.
  - **One result, one story about a pair.** `diff_pairs[].coupling` no longer says "sequential ... not
    gap-controlled" about a pair that `constraints.pairs` reports as coupled lanes, and its lengths are the same
    figures (via height included).
  - **Measuring the same copper again gives the same figures.** A call on an already routed board reported 82 %
    coupled / 3.8 mm uncoupled / `lanes: false` for copper the routing call had reported as 84 % / 3.4 mm /
    `lanes: true` (KiCad: 3.4008 mm): the parallel test did not survive the 0.1 um rounding of coordinates read
    back from the board file, and `lanes` was a record of the run instead of a property of the copper.
  - Copper the length tuner adds stays out of the plan's `forbid` / `avoid` areas with its whole width, not
    only its centre line.

- `pcb_route_guided` (engine "v2") now **routes differential pairs as coupled lanes**, compensates their skew,
  lays per-layer widths and treats keep-short nets as such. A pair (from the plan, a KiCad rule, a net class or
  the net names `_P/_N`, `+/-`, `DP/DM`) whose pads are neighbours at both ends is routed as one object: both
  tracks leave their pads on a diagonal straight to their lanes, the lanes run `width + gap` apart from pad
  breakout to pad entry, and a layer change is two vias side by side. Every lane segment and via is checked as
  copper of its own net; the gap is the pair's but never below what KiCad allows between the two nets (for
  `USB_DP` / `USB_DM`, which KiCad does not read as a pair, that is the ordinary clearance — reported as
  `gap_seen`). Right after a pair is laid, the shorter net gets shallow bumps on its outer side until the pair is
  inside its skew limit (`diff_pairs[].skew` or a KiCad `skew` rule); bumps that would cost the pair KiCad's gap
  or uncoupled-length rule are taken out again. A pair is one object for rip-up too: repair never rips its lanes
  for another net, polish leaves them as laid, and when KiCad rejects one lane both are re-laid together. **A
  pair that cannot be coupled still routes, as two nets, and says why** (`constraints.pairs[].lanes: false` +
  `why`: pads swapped between the ends, no legal fan-in at a part, no path at the pair's width, connections that
  do not pair up end for end, low detection confidence; finding `PAIR_UNCOUPLED`). Completion comes first for
  pairs nobody declared: when a board does not end complete and clean with pairs detected from names or classes
  laid as lanes, it is routed once more with those pairs as two nets and the better result is kept and says so
  (a pair the plan declares with `coupled: true`, or a KiCad pair rule, always stays coupled). `nets[..].widths` (and a
  KiCad `track_width` rule with a layer) now reach the copper: the search runs at the widest layer's width, each
  layer's segments are laid at that layer's own, and such a net is never necked; a net-class width may neck at a
  pad that needs it and only there (`constraints.widths.necked` / `off`). Keep-short nets (plan `keep_short` /
  `max_length`, a KiCad `length (max ..)` rule, switch nodes, antenna feeds) are routed with the critical nets and
  report their length against the maximum, or their detour over the estimate. The plan's `max_length` is the
  contract its field text always promised: a net that comes out longer loses its longest-detour connection,
  which is reported as failed with the excess in mm (a KiCad max-length rule is reported, not enforced that
  way). `global.constraints: "off"` or `"report"` lays exactly the copper of before; boards without constraints
  route as before. Not yet: serpentines of a coupled pair as a pair, impedance computed from the stackup (`z`
  is a label), pairs on nets with a tee, per-layer widths for the two nets of a pair.

- `pcb_route_guided` (engine "v2") now **tunes length-matched groups**: after KiCad has judged the routed board,
  every member of a group that is shorter than its window gets serpentines on its own free straight runs until it
  is as long as the group's longest member (or `groups[].len`), within `tol`. The bumps are 45-degree chamfered,
  their spacing and smallest amplitude come from the track width and clearance (and the project's tuning-pattern
  settings), the largest amplitude from `groups[].amp` — without one the router starts at the project's value and
  grows it only when a member would otherwise stay short. Every bump is checked against all other copper, holes,
  rule areas, the board edge and the plan's forbid / avoid areas, keeps its distance to its own net, and the run
  that needs the fewest bumps is used. KiCad then judges the tuned board: a meander that would add a DRC error or
  open a connection is taken out again, net by net, and tried elsewhere; a net is never made longer than its
  window. What cannot be met is reported per group with the missing millimetres, the reason and how much the free
  runs could hold (`constraints.groups[].short_by`, `why`, `capacity`; finding `LENGTH_MISMATCH`) — never as a
  pass. Groups come from the plan or from a KiCad `skew` / `length` rule; a group guessed from net names is still
  only measured. The members of a tuned group are routed as one block, longest first, so the group's target is not
  inflated by a detour. `dry_run`, `compare` and rip-up behave as before (nothing is written on a dry run), and
  boards without such a group route exactly as before. Not yet: meanders on tracks that were on the board before
  the run, or of a coupled pair as a pair (skew inside a pair and coupled routing: the entry above).

- `pcb_route_guided` (engine "v2") knows electrical constraints and reports them: length-matched groups,
  differential pairs (skew, uncoupled length, gap), width / impedance classes and keep-short nets. The server calls
  no model: the plan declares them (`groups: [{nets, tol, len, amp, name, off}]`, `diff_pairs[]` + `skew`,
  `uncoupled`, `coupled`, `z`, `nets[..].widths` / `z`, `global.constraints: "auto" | "plan" | "report" | "off"`),
  and what the plan leaves out is detected from KiCad's custom rules (`.kicad_dru`: `length`, `skew`,
  `diff_pair_gap`, `diff_pair_uncoupled`, `via_count`, `track_width`), net classes and net names (`_P/_N`, `D+/D-`,
  `USB_DP/USB_DM`; SD, QSPI, RGMII, DDR, ULPI, video and clocked buses). The result gains `constraints`: per group
  the target, the spread and by how many mm the worst member is short or long; per pair both lengths, the skew, how
  much of it runs side by side at its gap; per keep-short net its length against the maximum — measured the way
  KiCad's DRC measures a net (tracks, the in-pad and in-via shortcut, via height from the stackup, pad-to-die) and
  with KiCad's own verdict where it checks the rule. `pcb_route_plan` lists what would be detected in
  `auto.constraints`. In this first step pairs, widths and keep-short nets were measured and reported only (coupled
  pair routing, width rules and keep-short handling: the first entry above; length tuning: the second) — a missed target is a finding with the amount (`LENGTH_MISMATCH`, `PAIR_SKEW`, `PAIR_UNCOUPLED`,
  `KEEP_SHORT_LONG`, `WIDTH_NOT_HELD`), never a silent pass. A group guessed from net names alone is measured,
  never enforced, and comes with the plan patch that adopts it (`CONSTRAINT_GUESSED`); a KiCad rule whose condition
  the router cannot read is named (`RULE_NOT_READ`). KiCad's length / skew / pair errors no longer make the router
  rip a connection up and remove it (a net is not made longer by removing it); they are counted in
  `drc.constraint_errors`. Boards without constraints route exactly as before. `copper_help("pcb_route_guided")`
  has the syntax and the report format.

- `pcb_route_guided` routes with the staged engine ("v2") by default: power reservations, fine-pitch fan-out, global
  corridors, detail search, repair, polish, and KiCad's DRC as the judge. `plan.global.engine: "guided"` selects the
  classic single-pass router; both read the same plan and keep the same safety rules. Under either engine a
  `regions[].forbid` area, a net's `avoid` rectangle and a strict corridor are never routed through (in v2 they are
  rules of the exact checker and every piece is checked against them again before KiCad sees it); `dry_run`,
  `compare` and a rejected plan never write; a run that closes no connection leaves the board file byte-identical —
  a rip-up is undone and reported as `ripped_up: 0` with `not_written`; a ground net with 6+ pads and no pour is
  left for `pcb_pour` and listed in `skipped` with its connection count. The default result is compact (counts,
  `failed` with reasons, findings with fixes, `suggest` patches); `detail=true` adds the engine's stage reports,
  judge rounds and every note. `pcb_route_plan` returns `auto` (what the engine would decide by itself) and `cuts`.
  For a ground net left for the pour the engine keeps the thermal-relief spokes that pour will need free (a track
  through one was a `starved_thermal` error after `pcb_pour`), fan-out escape vias the routes did not use are
  removed (KiCad reported them as `via_dangling`), and a run with nothing but pour pads to connect no longer
  repeats the whole pipeline for the via-frugal trials. `board_build` on a small board now takes about 10 s longer
  than with the classic router and may return `pending: ["verify"]` once at its 45 s time box.
- `verify`: `next` is a call also when the board is fully connected but DRC errors remain (the first error's own
  fix, or `render(target='pcb')`), instead of the prose "fix DRC errors (see findings[].fix)".

- Stock symbols first, and only when they are right. Placing or importing an LCSC part (`sch_apply` place `lcsc` / `spec`,
  `parts_import`) now uses KiCad's own symbol whenever one passes a pin-by-pin check against the supplier's pin data:
  symbols named after the MPN or its family (packaging suffixes, `x` wildcards, `LM358DR2G` → `LM358`, vendor prefixes),
  and generic symbols picked from the real pinout (`Q_NMOS_GSD` / `Q_NPN_BEC` by pin order, `Crystal_GND24`,
  `Conn_01xNN` / `Conn_02xNN_<numbering>`, `D_Bridge_+-AA`, `Device:L` / `D` / `LED` / `C_Polarized` with polarity).
  Checked: pin numbers cover the footprint pads exactly (stacked `[1,15,38,39]` numbers expanded), names and directions
  agree, multi-unit symbols match the supplier's channels, and a stock footprint is used only when its pads lie on
  the supplier's pads with the same pitch and numbering — otherwise the stock symbol sits on the converted footprint.
  A TO-92 symbol is no longer given to a SOT-23 transistor (S8050, S8550, 2SC1815), a reverse-pinout regulator
  (AP7361C-33ER) no longer gets the forward symbol, and a unidirectional TVS no longer gets the polarity-less `D_TVS`.
  Every result says what was chosen and why: `symbol_source`, `footprint_source`, `why`, `selection` (checks passed,
  candidates rejected); the converted symbol is an explicit fallback with its reason (`selection.fallback`).
  Measured (bench/parts/stock_coverage.py): 147-part corpus 50 % → 83 % stock symbols, picks its audit flags as wrong 5 → 0; bundled
  Basic/Preferred table 77 % → 89 %.

- `parts_search` finds long-tail parts by description ('alcohol gas sensor', 'wifi module', 'sd card socket'): words are
  matched as whole words against description + catalogue category, the live catalogue is asked in its own wording
  ('gas sensors') and by category, a result that confirms only some words is `match: partial` with `unconfirmed`, and
  a short family name ('MQ-3') no longer matches unrelated part numbers; out-of-stock hits are listed in `out_of_stock`.
- `design_review` no longer asks for decoupling on loads drawn like ICs: a gas-sensor heater (MQ-x `H` / `VH+` pins,
  which KiCad types as power inputs), a motor, fan, coil or LED. A supply pin now needs the right electrical type or
  name, a part with electronics (a signal pin or its own ground pin) and a real rail; a heater's switched node is
  not listed as a power rail.
- `fab_export` checks stock: with assembly it asks the parts provider for live stock of every BOM part (for `boards` boards, default 5) and
  reports parts with zero / too little stock or not listed as a `warnings` entry (code `stock`: affected references and a
  ready `parts_alternatives` fix per part). It never blocks; `waive=['stock']` acknowledges it. Offline or when the
  supplier cannot be reached the pack is still written and `stock.not_checked` plus the first order note say stock was
  NOT checked. `summary.assembly_fees` and one order note now spell out Basic / Preferred / Extended counts, the fee
  total and which references cause it.
- Locked footprints: `pcb_edit` ops `lock` / `unlock` (`refs` or `all`), and `lock` / `unlock` on `pcb_place` move items.
  An explicit move of a locked part is refused with `LOCKED` and a ready fix instead of being dropped silently;
  `"unlock": true` on the move (or `lock: false`) moves it, and `{"ref": "U2", "lock": false}` now really unlocks.
  `pcb_place(auto=true)` lists every part it left alone in `skipped` with the reason and a fix (`PARTS_SKIPPED`).
- Footprint-owned Edge.Cuts: footprints converted from EasyEDA no longer carry graphics on Edge.Cuts (e.g. a USB-C
  plug's "board edge here" line, which KiCad counts as board outline). The importer moves them to Dwgs.User and says
  so (`edge_cuts_moved` + a warning with where the board edge belongs). `verify` DRC adds `footprint_edge_cuts` for
  boards that already have such a footprint: an error when the graphics are open, a warning for a closed cut-out.
- `sch_tidy` now draws in style `pro2` by default: canonical drawings per idiom (LDO / boost line, op-amp loop,
  crystal, LED chain, divider), wires inside a block, frame columns in signal-flow order, generated design notes and a
  rails line in the title block. The guarantee is unchanged (nothing is written unless KiCad's netlist of the whole
  project is identical and ERC reports nothing new) and so is the fallback ladder `pro2` → `pro2-safe` → `pro` →
  `pro-plain` → `labels`: `style_used` names the style that was written, and when it is not the one asked for the new
  `fallback` field (also in the summary line) says why the better ones were refused; `fallbacks` lists each refused
  style. `style='pro'` and `style='labels'` stay selectable. The tool's listed description is shorter (drawing rules,
  ladder and symfix detail are in `copper_help(tool='sch_tidy')`). The re-layout `sch_apply` does on its own after a
  position-free batch still draws `pro`; the first `sch_tidy` on such a sheet may redraw it once in `pro2`, the next
  answers `unchanged`. `bench/sch/tool_e2e.py` runs the tool end to end on copies of the bench designs (and of any
  project given with `--project`) with an independent netlist / ERC witness. (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md §19)

## [1.0.0] — 2026-11

### No vendor, fab or model is assumed
- **Config.** One layered settings loader: built-in defaults < `$COPPER_HOME/config.json` <
  `<project>/.copper/config.json` < call arguments. Keys: `parts.providers` (ordered), `parts.policy`,
  `parts.catalogue`, `parts.provider_paths`, `fab.profile`. A broken file gives `BAD_CONFIG` with file, line and fix.
  `copper_status`, `project_summary` and `copper doctor` show the active providers and fab profile. (docs/CONFIG.md)
- **Parts providers.** Tools talk to a provider interface. Built in: `lcsc` (the default; everything Copper did
  before: offline Basic table, live JLC stock, EasyEDA import), `local` (a CSV/JSON catalogue mapping MPN / value /
  package to a KiCad symbol + footprint and any supplier numbers; fully offline) and `kicad` (generic library parts).
  Third-party providers load from the entry-point group `copper_kicad.parts` or a path in the user config.
  `parts_search` / `parts_get` / `parts_import` / `parts_source_bom` / `parts_alternatives` take `provider=` and report
  which provider answered; `sch_apply` place and swap accept `part` (+ `provider`). Placed parts always carry `MPN`
  and `Manufacturer` when known. (docs/PROVIDERS.md, with a complete example provider that the tests run)
- **Fab profiles.** `fab_export(profile=…)` writes the order pack for a fab profile. A profile is JSON: design-rule minimums, Gerber and
  drill settings, file naming, zip layout, BOM and placement columns, rotation table, part-number field, what blocks
  an order. Built in: `jlcpcb` (default), `pcbway`,
  `oshpark`, `generic` (Gerber X2 + Excellon + KiCad position file + plain BOM). Users add profiles in
  `$COPPER_HOME/fabs/` or `<project>/.copper/fabs/`; `fab_export(profile='list')` shows them. Each profile's notes say
  which numbers were checked against the fab and which were not. (docs/FAB_PROFILES.md)
- `project_create(fab=…)` and `pcb_setup(rules='fab' | '<profile>' | '<profile>_advanced')` take their design rules
  from the profile; `jlc-2layer` / `jlc-4layer` / `rules='jlc'` name the JLCPCB profile. The waive code for unsourced parts is
  `parts`; the release prompt is `release_to_fab`; the sourcing and release skills are `parts-sourcing` and `fab-release`.
- Server instructions, tool descriptions, prompts and the guide no longer name a model or a client.

### Professional schematic layout with a netlist-identity guarantee
- `sch_tidy` re-draws a sheet as titled functional blocks in signal-flow order (power input, regulator, MCU, other
  ICs, connectors): pull-ups above and pull-downs / RC caps / switches below the pin wire they serve, series parts
  and LED chains in line, decoupling caps as a bank at their IC, neighbouring rail pins on one rail wire with one
  power symbol, labels only where a net leaves its block, PWR_FLAGs parked in the power block, smallest paper that
  fits, title and date filled in. New options: `style` (`pro` | `labels`), `route`, `blocks` (titles, notes, member
  hints), `frames`, `title_block`, `paper`, `force`.
- Nothing is written unless it is proven on staged copies of the whole project (never inside the project folder):
  KiCad's netlist of the root sheet identical (every component field, net name, net class, pin function and type),
  symbols unchanged except for their position, hierarchical / global labels, PWR_FLAGs and no-connect flags on the
  same nets, the same set of names on every net, and no new ERC violation. The file that is proven is the file
  that is written (KiCad's own re-save happens before the check). Refusals: `LAYOUT_REFUSED`, `LAYOUT_UNSUPPORTED`.
- A sheet somebody wired by hand, or that carries notes or graphics, is left alone (`style_used: "kept"`) unless
  `force=true`. Sheets with hierarchical sheet symbols or buses, and sheets used by several instances, are refused.
- `sch_apply(layout="auto")` re-lays a sheet after a batch only while the layout engine is the only one who has
  arranged it and the batch gave no explicit position; `layout="keep"` switches that off. This holds for every
  batch, not only the first one; `wire` between two pins and `label` on a pin count as position-free.
- A decoupling cap stays with the IC it was placed at (`near`, or the block it is already drawn in). A chain that
  starts on a supply rail (+5V - R - LED - GND) is drawn in line.
- With `force=true` the user's own text notes are stacked under the blocks instead of being left on top of the new
  drawing (`your_notes` in the result); lines, boxes and images stay where they were and are reported.
- `sch_tidy` drawing rules, second pass: PWR_FLAGs sit on the net they mark (beside the rail symbol / on the wire) instead
  of in a strip; decoupling caps stand at the supply pin on the pin's own rail wire (top and side pins); supply and
  ground symbols stay upright wherever a short wire makes that possible; a net wired in one piece carries its name on
  the wire; output caps hang at the end of the inductor; frames are packed on the smallest sheet (rows inside a
  column, connectors under the block they serve), share column edges and titles say what a block is for (`LDO  VBUS →
  +3V3`, `BATTERY CHARGER  TP4056`); the sheet title is written as a title. Readability floors moved with it: the
  score now also counts sideways supply symbols, vertical names and off-centre drawings; rp2040-min part distance
  45 → 49 mm (caps in one row at the IOVDD pins), li-ion-powerbank fits A4. `bench/sch/run.py` is the benchmark.
- `sch_tidy` drawing rules, third pass: a connector of up to six pins that only talks to one IC (motor terminals, a
  debug / programming header, an SPI header) is drawn in that IC's block, facing the pins it serves and wired row on
  row where the rows line up - no more near-empty connector frames; neighbouring pins of one signal on one side of a
  part (USB-C D+ / D- on A6 / B6, A7 / B7) get a short jumper at the pins and one name; a decoupling bank wraps into
  rows of at most six caps; a small block is no longer stretched into a mostly empty frame; drawings are centred in
  the area above the title block; the sheet only goes one paper size up when that really untangles the flow (frames
  lose a little inner air first). Fixed: a net name could be put on a point where a foreign wire crosses (KiCad then
  joins both nets; the candidate was refused by the pre-check and the sheet fell back to the plain style).
- Running `sch_tidy` again on a sheet it has already drawn answers `style_used: "unchanged"`: no write, no
  checkpoint.
- `sch_read(query="readability")`: overlaps, crossings, labels per net, wired share, paper fill and a 0-100 score.
- Labels are moved, never re-created (their fields, fonts and uuids survive); block frames are KiCad groups named
  `copper:block:<ref>`, so a later run replaces them instead of stacking them.

### Setup, linking and coming from KiCad
- `copper setup` opens a setup page in the browser, served from 127.0.0.1 in Copper's colours: KiCad found, what
  to bring along from it, AI apps to connect, GitHub (optional), the autorouter download and a real test call.
  The Windows installer opens it from its last page through a new windowless launcher (`copperw.exe`), and adds
  "Copper setup" to the Start menu. `copper setup --tty` (and any session without a desktop) keeps the terminal
  wizard.
- Coming from KiCad: setup lists existing KiCad installs and settings versions and asks, item by item with the
  default shown, whether to bring along symbol and footprint library tables (with your own libraries), path
  variables, colour themes, hotkeys, plugins, recent projects, a project's design rules and net classes, and
  whether to enable KiCad's IPC API. Settings of an older KiCad are copied into the new one and only read at the
  source. Every changed file is backed up and `copper setup --undo` puts everything back. `copper setup --yes`
  no longer changes anything in KiCad's own settings (it used to create library tables and enable the API);
  `copper setup --yes --import sym_libs,fp_libs,api` does so explicitly.
- `copper link`: every AI app on the machine with its real state (connected, broken, found, not found), one
  answer to connect them all, `--verify` (starts the registered command and performs the MCP handshake),
  `--list`, `--unlink`. `copper doctor` now fails a client whose registered command does not start.
- `copper link github` (optional): uses the GitHub CLI's login when `gh` is installed and then stores no token;
  otherwise GitHub's device sign-in with a client id from `COPPER_GITHUB_CLIENT_ID` (none is shipped). A token is
  kept only in the OS credential store (Windows Credential Manager, macOS Keychain, libsecret).
- `copper uninstall --accounts` and `--purge` (removes only the names Copper creates in its data folder).
- macOS and Linux: hardened `installer/install.sh` (https only, checksum check against the release's
  `SHA256SUMS.txt`, no root, private temp dir), a Homebrew formula, an AppImage recipe and pipx instructions
  under `installer/`. These are untested on real macOS/Linux machines.
- Finding running KiCad processes takes about 50 ms instead of 3 s (`tasklist` replaced by a process snapshot).
- A sandboxed server (`COPPER_CONFIG_HOME_OVERRIDE`) keeps its state in the sandbox: the test suite no longer
  writes its recent projects into the real `~/.copper/state.json`.
- Looking at KiCad no longer creates or re-saves its settings: the version check, the capability probe and
  `copper doctor` run `kicad-cli` with a throw-away config folder. Before, installing Copper ahead of the first
  start of a new KiCad created `<config>/10.0/`, which makes KiCad skip its own "import settings from a previous
  version" offer.
- KiCad import hardening: two imports in the same second no longer share (and overwrite) one backup folder; undo
  never rewrites a settings file it cannot parse; a library table in another layout keeps its entries instead of
  being replaced by the default table; a damaged table is reported and left alone; a file that cannot be
  replaced leaves no `.copper-tmp` file behind; a damaged journal is set aside, not overwritten; after KiCad or
  you saved a library table again, undo removes exactly the entries Copper added; undo removes files and folders
  Copper created; `copper uninstall --purge` keeps the backups of an import that was not undone.
- Setup tells you when an older KiCad still has libraries, themes or plugins of your own that the new one does
  not (`copper setup --from 9.0`).
- `copper link --all` connects every app found. It used to disconnect everything.
- The launch check scrubs token-shaped text from a failing server's last output and reports its exit code.

### KiCad compatibility
- Copper now knows which KiCad it is talking to. `copper doctor` lists every installed KiCad with its version,
  channel (stable, release candidate, nightly, old), the file formats it reads, whether it can edit boards and which
  one is active and why; it warns when a newer KiCad 10 release exists and what it fixes (10.0.7 fixes zone fills
  that can short pads). `copper_status` reports the same for the active install as `kicad.advisories`.
  See `docs/COMPATIBILITY.md`.
- A KiCad nightly installed next to a stable release no longer becomes the active KiCad, and board edits always
  run on the Python of the KiCad whose kicad-cli checks them (`ENGINE_MISMATCH` otherwise).
- A project saved by a newer KiCad than the active one is refused with `FORMAT_TOO_NEW` (naming the file and the
  version that saved it) instead of failing without a reason or, for a sub-sheet, being silently left out of the
  netlist and ERC. `copper_status`, `project_open` and `checkpoint` keep working on it.
- Designs saved by an older KiCad are checkpointed before their first edit, and the result says once that the file
  is now in the newer format (`format_upgraded`). A pre-release KiCad never upgrades a file without
  `project_open(path, allow_format_upgrade=true)`.
- Checkpoints now hold sub-sheets kept in sub-folders of the project and restore them; before, an edit (or the
  one-way format upgrade) of `sch/power.kicad_sch` had no backup. A format upgrade is reported only after the
  file has really been saved in the newer format.
- A kicad-cli that does not run is refused with `KICAD_CLI_BROKEN` before anything is written; `project_open`
  no longer reports "0 components" when KiCad could not read the schematic.
- kicad-cli flags are checked against the binary's own `--help`: a flag an older KiCad lacks is left out and the
  result says so, instead of the whole command failing.
- KiCad 9 is view/verify only (`KICAD_TOO_OLD` on edits). The README said "KiCad 9 or 10"; Copper writes KiCad 10
  files, which KiCad 9 cannot open.
- Board workers no longer hang for minutes on a board that uses a font that is not installed (pcbnew showed a
  modal dialog nobody could see).
- Board workers no longer create and use an empty `kicad/10.0/10.0` config folder; `copper doctor --fix` removes a
  leftover one when it is empty.
- ERC marker positions are scaled by measurement (KiCad up to 10.0.6 reports them 100 times too small, 10.0.7
  fixes it), so they are right on both.
- Symbol libraries stored as a folder of one-symbol files are searched and read; `api-<pid>.sock` live-API
  sockets are found.

### Fixed (pre-release hardening)
- Distribution and onboarding, from the fresh-user and dogfood passes:
  - A client config with an invalid JSON escape (a Windows path typed with single backslashes) crashed
    `copper doctor`, `status` and `setup` with a traceback. It is now reported as *not valid JSON* with the file's
    line and column and a hint to double the backslashes; the other checks keep running.
  - `copper doctor` showed EasyEDA as healthy on HTTP 403. It now probes with the headers the import uses, treats
    401/403/407/429 as warnings, and also probes the jlcsearch mirror that `parts_import` falls back to.
  - A default install no longer looks complete without the autorouter: Freerouting is listed under *PCB engine*,
    doctor ends with an explicit `copper freerouting --install` line, Setup ticks Freerouting by default and the
    wizard offers it with *Yes* as the default.
  - The Windows installer can register GitHub Copilot CLI, and lists AI apps that are not installed yet (unticked)
    instead of hiding them. The finish page and quick-start give the `copper install --client <id>` command for
    apps installed later.
  - `copper setup` no longer claims *Nothing is changed without asking* under `--yes`; the one-line installers run
    the interactive wizard when there is a terminal and use `--yes` only without one (CI).
  - Bare `copper` in Git Bash/mintty shows the help instead of silently becoming the MCP server; when it does
    serve, it says so on stderr.
  - `COPPER_CONFIG_HOME_OVERRIDE` (test mode) now also sandboxes KiCad's config (`KICAD_CONFIG_HOME`) and
    `COPPER_HOME`, so `copper setup` in test mode cannot touch the real KiCad settings.
  - `copper freerouting --install` with a custom `COPPER_HOME` reuses an existing `~/.copper/tools` install
    instead of downloading ~100 MB again; doctor shows which tools folder is used.
  - `copper uninstall` deletes config files that Copper itself created once nothing else is left in them, and
    lists the `*.copper-backup` files it keeps.
  - `copper --help` no longer lists the hidden `_winpath ==SUPPRESS==` command.
  - `copper config --client claude-code` prints separate PowerShell 7.3+ and 5.1 commands.
  - `copper doctor` warns when Claude Code has both the Copper plugin and a user-scope `copper` server.
  - The `.mcpb` `entry_point` (`src/copper_kicad/__main__.py`) runs as a plain script; `.mcpbignore` is no longer
    shipped inside the bundle.
  - Codex plugin manifest uses `interface.defaultPrompt`; the docs no longer mention a `codex plugin add` command.
  - `scripts/build_release.py` checks the wheel byte for byte against `src/`, clears old artifacts, writes
    `dist/BUILD-INFO.json` and has `--check` to fail CI when `dist/` is older than `src/`.
    `scripts/check_release_urls.py` checks every public URL and the PyPI package before announcing.
- Upgrading or uninstalling while an AI app was running Copper left a broken environment (`copper.exe` present,
  package deleted) and the installer still reported success. Setup, `install.ps1` and the portable `install.cmd`
  now stop Copper processes running from the install folder (Setup asks first). They also confirm that
  `copper version` runs before reporting success.
- `install.ps1` aborted before `copper setup` on Windows PowerShell 5.1 because `uv` writes to stderr.
- `install.sh` no longer fails in CI or Docker, where `/dev/tty` is readable but cannot be opened.
- Fixes to client config handling:
  - A UTF-16 config no longer crashes `doctor`/`status`. Non-UTF-8 bytes survive a round trip unchanged.
  - Duplicate `"copper"` keys are collapsed; JSON keeps the last one, so editing the first changed nothing.
  - Configs that are symlinks are written through rather than replaced.
  - A read-only config is reported clearly, and no `*.copper-tmp` file is left behind.
- Codex `config.toml` fixes:
  - Keys and subtables you added under `[mcp_servers.copper]` (`enabled`, `env_vars`, `[…env]`) are kept.
  - An inline `copper = {…}` or dotted definition is replaced instead of duplicated; the duplicate made the file
    invalid.
  - On Python 3.11+, the result is checked with `tomllib`, and an invalid file is never written.
- Fixes to install detection:
  - A repeat `copper install` is idempotent with entries written by `claude mcp add-json` (`"env": {}`).
  - `CLAUDE_CONFIG_DIR` is respected.
  - `uvx copper-kicad setup` registers `uvx`, not a path inside uv's cache.
- `copper setup` and `copper kicad-api enable` no longer create a new KiCad version's config folder before that
  KiCad has started once. Creating it early silently skipped KiCad's "Import settings from previous version"
  step, so custom libraries were lost.

### Added
- MCP server (stdio, protocol revisions 2024-11-05 → 2025-11-25) with tools for the schematic, parts (LCSC/JLCPCB),
  verification, rendering, PCB and fabrication. Standard library + Pillow only.
- `copper` CLI: `serve`, `setup` wizard, `install`/`uninstall` for Claude Desktop (including the Microsoft Store
  build), Claude Code, Codex, Cursor, VS Code, Windsurf/Devin, Zed, Gemini CLI and GitHub Copilot CLI, plus
  `doctor`, `status`, `config`, `render`, `kicad-api`, `freerouting`.
- Format-preserving config edits: comments, ordering and other servers are left untouched, and each file is backed
  up once to `*.copper-backup`.
- Windows installer (per-user, no admin) built with NSIS. Its page flow follows KiCad's installer, and it offers
  optional components for each detected AI app, the KiCad IPC API and Freerouting.
- One-line installers for PowerShell and macOS/Linux shells, a Claude Desktop `.mcpb` bundle, a Claude Code /
  Codex plugin marketplace with four skills (`kicad-design`, `parts-sourcing`, `design-review`, `fab-release`),
  and MCP Registry metadata (`server.json`).
