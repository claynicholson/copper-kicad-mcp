# Board outline from a CAD file, and the elegance score

Two things live here: how `pcb_setup(outline_file=...)` turns a mechanical file into the board
outline, and how Copper measures and improves the quality of a layout. Both are deterministic local
code: no model is called, nothing is uploaded, and no fab or vendor is assumed.

## 1. Outline from a CAD file

```
pcb_setup(outline_file="mechanical/case.step")              # or 'auto' = the one CAD file in <project>/mechanical/
pcb_setup(outline_file="board.dxf", outline_dry_run=true)   # look first, write nothing
python -m copper_kicad.cad.outline case.step --png pick.png # the same analysis without a board
```

| Format | What is read | Arcs | Units | Notes |
|---|---|---|---|---|
| STEP `.step` `.stp`, STEP-Z `.stpz` (gzip or zip) | planar faces of every solid, assembly placements | true arcs and circles; splines re-fitted as arcs | from the file (mm, cm, m, inch, foot) | board body or enclosure cavity is picked automatically |
| DXF `.dxf` (ASCII) | LINE, ARC, CIRCLE, LWPOLYLINE / POLYLINE with bulges, SPLINE, ELLIPSE, INSERT (one level) | true arcs | `$INSUNITS`, else mm assumed (`outline_units`) | `outline_layer` picks a layer; layers named outline / edge / board / profile ... are preferred |
| SVG `.svg` | path, rect, circle, ellipse, line, polyline, polygon, nested transforms | circular path arcs are kept; Beziers are flattened to 0.005 mm and re-fitted | `width` + `viewBox`, else 96 dpi (`outline_units`) | `outline_layer` = a group id or label; stroke widths, `use`, CSS and text are ignored |
| STL `.stl` (ASCII or binary) | triangles grouped by plane | re-fitted from the tessellation | none: mm assumed (`outline_units`) | within about 0.05 mm of the source |

Not read: IGES, 3MF, OBJ, DWG, binary DXF, native CAD files (the error names the format). Files over
40 MB are refused before parsing; export only the board or the enclosure base.

What is written: board-level Edge.Cuts lines, arcs and circles (never one segment per tessellation
step), cutouts, and stock mounting-hole footprints at circles of 2.2 / 2.7 / 3.2 / 4.3 mm
(M2 / M2.5 / M3 / M4, within 0.25 mm). Other hole sizes become Edge.Cuts circles. Smaller circles
and narrow slots of an exported board are component drills: counted, not imported
(`outline_holes='all'` cuts them too). The netlist, footprints, tracks and zones are never touched;
the board thickness found in the file is reported, not written.

### Picking the face (3D files)

All planar faces of one solid that lie in one plane form a *plane group*; its outline is the union
of the faces, so a board top that a CAD system split into dozens of faces still gives one outline.

1. **Board**: two opposite parallel groups of one solid with equal extents, 0.2 to 6.5 mm apart.
   Inner contours count as holes only when they exist on both sides (anything else is a part fused
   to the board). Confident only for a standard PCB thickness and a short side of 8 mm or more.
2. **Enclosure**: the largest recessed group that is open above, tested along every axis of the
   model. Floor features are classified: a window through the floor is ignored, a round boss with a
   pilot hole becomes a mounting hole at its centre, ribs taller than the board seat become cutouts,
   lower ones a footprint keep-out on the bottom side. The outline is the cavity shrunk by
   `outline_clearance` (default 0.5 mm). A plate inside the cavity is the PCB (board mode); a plate
   on top of it is a lid.
3. **Largest plane** otherwise.

The result always says what was picked. When the evidence is weak (`OUTLINE_AMBIGUOUS`: an
enclosure that was guessed, a plate that is not PCB-like, two candidates of similar size, unresolved
assembly placements) the candidates are listed with ids for `outline_pick`, and the picture of the
pick is returned even with `render=false`. `outline_mode='board' | 'enclosure' | 'face'` and
`outline_axis` override the guess. The enclosure heuristic was developed on synthetic trays; expect
real enclosures with draft angles, ledges and snap fits to need a look and often an `outline_pick`.

### Provenance

The board carries one property, `COPPER_OUTLINE` (visible in KiCad as a text variable):
`src=file|auto|user`, for an enclosure also `mode`, `top` and `bottom` (height limits in mm), and
`placed=auto` while every part sits where `pcb_place(auto=true)` put it. `pcb_read` shows it as
`outline.src`. A board without it is treated as user-defined.

## 2. Elegance

`verify` reports `elegance: {score, worst[]}`; `pcb_place` and `pcb_route` report the score after
their change; `pcb_read(what=['elegance'])` returns every measured number.

| Sub-score (weight) | Measures |
|---|---|
| space (20) | utilisation (only when Copper chose the size), emptiest third of the board, largest bare area (an empty rectangle, counted up to 2:1), balance of the part area |
| connectors (12) | edge-marked and side-entry connectors flush with the edge, headers along an edge |
| order (10) | rotations at 0/90, parts on a 0.25 mm grid, two-pad parts lined up with a neighbour; four or more identical large parts (pots, switches, sockets) at one rotation and in line with each other |
| proximity (12) | decoupling cap to its supply pin (3 mm good, 8 mm bad), crystal and load caps |
| economy (14) | track length against the pad-to-pad minimum, vias per routed net |
| geometry (12) | share of 135 degree bends, off-45 track length, acute angles, pad entries on centre |
| planes (6) | pour coverage, fill islands, tracks crossing under fast signals on a plane layer |
| silk (10) | references that overlap a pad, another reference, another part's artwork, sit under a part, off the board, under 0.8 mm or tilted; a hidden reference counts as half a bad one |
| holes (4) | mounting holes mirrored about both centre lines |

Sub-scores that do not apply are left out (an unrouted board is scored on placement). **Gates**:
any unrouted connection, DRC error or footprint outside the outline (mounting holes included) caps
the score at 49, using the same numbers `verify` reports; `ungated` is always shown beside it. A
courtyard overlap gates through DRC (`courtyards_overlap`); Copper's own bounding-box estimate is
a gate only when no DRC result is at hand, because it also fires on rotated, L-shaped and
two-sided parts that do not touch. Hints (`worst`)
are calls that exist and are safe on the board as it is: nothing proposes re-placing a routed board
without saying so, and a user-sized or imported outline is never told to resize.

What the tools do with it:

* `pcb_setup(auto_size=true)` (and `pcb_place(auto=true)` on a board with no outline) sizes the
  board from the parts: part courtyards at about 52 % of the area, one verification placement per
  aspect ratio (1.25 and 1.6), 8 % growth when something does not fit, then 5 % margin per side.
  Refused on boards with tracks, zones or locked parts; reports `AUTO_SIZE_FAILED` and changes
  nothing when no size fits within 20 s.
* `pcb_place(auto=true)` runs up to six structured candidates within a time budget (main connector
  on the short or the long edge, tighter or looser spacing, main IC rotated or pulled toward the
  connector), scores each and writes the best. Side-entry connectors sit with the body flush with
  the edge (`overhang={"J1": 1.2}` to stick out), clamped so pads and holes keep their edge
  clearance; decoupling caps must end within 4 mm of their pin (neighbours are evicted and
  re-placed, else `DECAP_FAR`); near-aligned two-pad parts are snapped into rows; on outlines with
  arcs or cutouts the keep-in test uses a 0.25 mm occupancy bitmap. Four or more identical large
  parts are taken off and put back as one row or matrix at one rotation when a legal block exists
  near where they were (`pcb/arrays.py`); otherwise they stay as placed.
* Mounting holes drawn in the schematic (a `MountingHole...` footprint with a symbol) are seated by
  `pcb_place(auto=true)`: the four corners first, then the middle of the long edges
  (`pcb/holes.py`). `pcb_setup(holes=...)` and auto-size add no board-only holes on top of them and
  say so. Locked holes and holes made by `pcb_setup` stay where they are. A footprint is a hole when
  its name or library starts with `MountingHole`, not when the word appears somewhere in a
  connector's name.
* `pcb_route()` then polishes only the tracks it created: overlapping same-net vias are merged,
  undersized via rings grown to the minimum, collinear segments merged, 90 degree corners chamfered,
  off-centre pad entries stubbed to the centre. Every step is undone in memory if a net loses a
  connection, and the board is restored byte for byte if DRC errors or unrouted connections rose.
  `pcb_route(mode='polish')` does the same on every unlocked track on request.
* References that are unreadable by the definition above are re-seated next to their part (0 or 90
  degrees). Readable and locked references are not moved. Only on a layout that is entirely
  Copper's own (`placed=auto`) may a reference that fits nowhere be hidden, at most 15 % of them.
* `pcb_pour` turns a thermal relief that cannot form two spokes (a pad squeezed by the board edge)
  into a solid connection instead of leaving a `starved_thermal` DRC error.

### Limits

The weights are a first calibration. Not measured: signal flow, thermal layout, test access,
differential pairs. The score is generous: a layout with every part legal, connectors on edges and
tidy tracks reaches 90 and above while still looking sparse in places, because nothing spreads
parts into free board area. Arc tracks are counted as odd-angle bends, and a dense two-sided board
whose designer hid most references loses the silk points. An outline taken from a STEP export of a
board with castellated or edge-plated holes keeps the half-holes as scallops in Edge.Cuts. The candidate search is a handful of structured variants, not a global
optimiser, and placement time grows steeply with part count (above about 60 parts expect one or two
candidates). Teardrops are not added (KiCad 10.0 cannot build them from Python). The polish steps
that re-route (acute-angle repair, rubber-banding, via removal) and the 3D-model height check are
not implemented. Connectors on a curved or slanted edge of an imported outline are placed by search,
not snapped to that edge.
