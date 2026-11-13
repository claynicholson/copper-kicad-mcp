# Router v2 — interface notes

Append-only log of changes to (or problems with) the interfaces in `ROUTER_V2_DESIGN.md`. One section per work
package; the design doc stays the single source of truth and is updated in place by the owner of the interface.

## W0 (types, oracle, stubs) — stage 0

Design doc sections changed in place: §3 (types block), §4.1 (oracle rules "as built and measured", gate result),
§4.7 and §4.8 (one sentence each, pointing at A1), §9 (oracle interface block), §13 (two risk rows), Appendix A1, A2.

**Changes to shared types (§3)** — all additive or measured:

1. `TOL_MM = 0.0003` (design said 0.001). Measured: KiCad 10 flags iff a gap is more than 0.5 µm short; 0.001 gave 124
   false-legal results on the seeded ladder. Applies to clearance / hole / edge only.
2. `Piece`: `drill`, `stage`, `protected`, `target_w` have defaults (0.0, "detail", False, 0.0 → `w`); `kind` other than
   'seg' / 'via' raises; `length` property. `Terminal` is a frozen dataclass with defaults for `pad`, `cluster`, `escape`.
3. `Score` is `@dataclass(order=True)` with zero defaults and `key()`. `Budget(total, parent=None)`, public `total` /
   `used`; `sub(fraction, floor)` = `max(floor, min(int(fraction × total), left()))`, charges go to every ancestor.
4. Constants added: `PROTECT_FACTOR`, `WORK_PER_SECOND`, `STAGE_SHARE`, `CHARGE_EXPANSION/SEARCH/WINDOW/PATTERN`,
   `ORACLE_CHECKS_PER_UNIT`, `ROLES`, `ROLE_ORDER`, `BLOCKER_RULES`.

**Changes to the Oracle interface (§9)**:

5. `Oracle(board, tol=TOL_MM, *, overrides=None)`. **W2 (engine)**: build it as
   `Oracle(board, overrides=oracle.local_overrides_from_file(pr.pcb))` — footprint-level clearance overrides and
   solder-mask margins are not in the worker's extract; without `overrides` they are not applied (the oracle is then
   too strict next to e.g. a QFN with a footprint clearance of 0.18, and has no mask-bridge rule).
6. `need(net, half_w, shape, layer, kind="seg", drill=0.0, clearance=None)`: `drill` is required for `kind="via"`
   (hole rules). Returns the copper-edge gap; `oracle.NO_NEED` (−inf) when the two never conflict (same net) — not 0.
   Keep-outs have need **0**, not infinity (copper may touch, not enter). **W3 / W4**: skip shapes with `NO_NEED`
   before adding `half_w`.
7. `via_ok(..., ignore=frozenset(), clearance=None)` — `clearance` added after `ignore` (keyword use recommended).
   `blockers(..., limit=8, clearance=None, ignore=frozenset())`.
8. Added: `seg_legal` / `via_legal` (bool, stop at the first violation, ~2× faster — use in inner loops),
   `piece_ok(piece)`, `via_blockers`, `near_soft`, `remove_soft` (reservations must be removable), `has_piece`,
   `set_clearance(net, mm)` / `clearance(net)` (plan clearance overrides: **W5/W9** call `set_clearance` once per net
   whose plan clearance exceeds the class; pieces added afterwards carry it), `net_id`, `inside`, attribute `checks`
   (callers charge `checks // 8` to the budget), attribute `mask_openings_hard`.
9. `seg_ok` / `via_ok` return the **worst** blocker (smallest `dist − need`), chosen independently of insertion order;
   `near()` returns dynamic shapes sorted by piece id (`shape.tag` = piece id; a via contributes a `cu` and a `hole`
   shape), outline segments as `cls == 'edge'`, hardened soft shapes as `cls == 'soft'`.
10. `soft_hits` reports soft shapes of *other* nets whose area the copper overlaps (need 0); a via is checked per layer
    with `x2 == x1, y2 == y1, w = size`. A reserver that wants clearance around a site must inflate the shape itself.
11. Module helpers: `local_overrides_from_file`, `raster_guard(pitch, reach)`, `dp_complement(name)`, `shape_gap(a, b)`.

**Findings other work packages must know** (measured, details in Appendix A1 / A2):

12. **W1 (`legacy.py`)** — the `one_air_max` self-check failure is *not* a snap/escape stub. It is an on-grid 45° step
    past a foreign pad corner (v1's grid EPS of 12 µm is smaller than the 23.7 µm dip of a diagonal step at a 0.15 mm
    grid), then `rip_net` drops the whole net. Of the two segments behind the 75 lost connections one is a real 2.3 µm
    violation and the other is legal (it fails only `seg_clear`'s extra 2 µm). So: rip the connection, not the net,
    and judge with `oracle.seg_ok` (rule − `TOL_MM`), not `Board.seg_clear` (rule + 2 µm). Expect about one such
    connection per pass on boards with a coarse auto grid; a re-queue with the offending cells blocked should fix it.
13. **W4 (`window.py`)** — `grow = need + half_width − TOL_MM + GUARD_MM` does not make steps between free cells legal:
    use `max(GUARD_MM, oracle.raster_guard(pitch, half_width + need))` per shape (or one value from the largest
    reach), or accept that emit will reject steps near corners. Pitch rule (a) `p ≤ (w + cl) / 2` gives a guard of up
    to ~0.11 × reach.
14. **W2 (`judge.py`, `kio.py`)** — observed once, not investigated: a track committed as net B that overlapped
    pad-connected copper of net A came back from KiCad as `Track [A]` and no short was reported. A short onto
    pad-connected copper can therefore surface as an open connection instead of a `shorting_items` marker. The oracle
    reports such overlaps as rule `'short'` before commit.
15. **W1 (`kio.extract`) / whoever touches the worker later** — not needed now, but each would remove an approximation:
    mask margins and footprint clearances in the extract (today read from the file text); copper graphics as real
    shapes instead of bounding boxes; custom pads with `ERROR_INSIDE` or a smaller arc error; kept arc tracks marked
    as arcs or sampled finer (today ≤ 10° chords, up to 0.4 % of the radius *inside* the true arc — the only
    approximation that is not on the strict side; it matters when routing next to kept arcs, i.e. `ripup != all`).
16. Mask-layer graphics are hard keep-outs for every net (v1 behaviour, kept). KiCad allows one net under an opening;
    37 human-routed reference segments on `one_air_max` and `stickhub` do exactly that. If completion near exposed
    copper matters, set `oracle.mask_openings_hard = False` and let S7 judge bridges.

**Stubs**: every other `v2/*.py` exists with the §9 signatures raising `NotImplementedError` and a module constant
`STUB = True`; replace the whole file when your work package lands and drop the constant. The stub `engine.py`
implements `available_stages()` (a module with `STUB = True` counts as absent) — W2 may keep or replace it.
`v2/__init__.py` imports nothing; `v2.MODULES` lists the module names.

**Running**: on this machine KiCad's `python.exe` ignored `PYTHONPATH` (Git Bash and PowerShell); the v2 tests insert `src`
into `sys.path` themselves, and `tests/test_v2_oracle.py` calls `bench/route/common.sandbox()` (scratch
`KICAD_CONFIG_HOME`, `COPPER_HOME`, temp dir under `bench/route/.work`). A temp dir whose path contains a space breaks
the Freerouting test in `tests/test_pcb.py` (use the 8.3 short path, as `common.sandbox()` does).

## W2 (judge, engine, session hook) — assumptions written before W1's files landed

Written first so W1 / W4 / W5 / W9 can see what `judge.py` and `engine.py` rely on; "as built" follows in the next W2
section. Nothing in §9 is renamed; these are the points §9 leaves open.

1. **`kio.commit_and_check(pr, scratch, tracks, vias, baseline_raw)`** — the judge owns the scratch file: before every
   call it copies its *base* file (the board without any v2 copper, ripped up as asked) over `scratch.pcb`, so
   `commit_and_check` must commit **into `scratch.pcb` as it is on disk** (commit → fill → save → DRC of `scratch`) and
   must not copy `pr.pcb` over it. `uuids` = track uuids, then via uuids, i.e. aligned with the third list of
   `RouteState.export()`. `new` = the new error markers; the judge only reads `m.get("type")`,
   `m.get("at")` (`[x, y]`), `m.get("message") or m.get("description")`, `m.get("items")` and hands the marker back to
   `kio.marker_pieces` unchanged. `unconnected` = `{net: n}` after the refill; `stats` is passed through to the result.
2. **`RouteState`**: the engine needs the failure record that `set_status(conn, 'failed', fail)` stored. §9 has no
   getter; `judge`/`engine` look for `state.fail(conn) -> dict | None`, then `state.fails[conn]`, then
   `state.failure(conn)`. Pieces with `conn == -1` are removed as a group: same-net `conn == -1` pieces that share end
   points.
3. **`legacy.route_all(ctx, budget, only)`** — when `classify` is absent the engine builds `ctx.conns` itself from v1's
   `Router(board, cfg, mode="signals").plan_connections()`: `ConnV2.idx` == v1 `Conn.idx` (same order), `rank` == v1
   `Conn.key`, `a`/`b` = the v1 pads (`Terminal.pad` = pad name, `cluster` = v1 cluster id). `board.grid` is made by
   the engine (`board.make_grid(cfg.step)`) before `legacy` runs. `only=[...]` re-routes exactly those connections
   (status `todo`) with everything else in `ctx.state` as fixed copper. Failed dicts in v1's format are read from
   `StageReport.data["failed"]` (list) and from the state (point 2).
4. **Judge → search feedback** (`ctx.history`, undefined in §3): the judge writes
   `ctx.history["marks"] = [[x, y, radius_mm, cost, net | None], ...]` (cost decays × 0.95 per judge round), and — so
   that v1's search in `legacy` and any search that honours plan regions sees it too — appends `guided.plan.Region`
   objects named `"judge"` to `ctx.cfg.regions` (cost region first; `forbid` for that net when the same marker comes
   back). A re-route that ignores both will reproduce the rejected copper; the judge then removes the connection.
5. **Pours pass before W5**: while `power.place_dogbone` is a stub, `judge.pours_pass` connects pads the refilled pour
   misses with v1's `Router(mode="pours")` on a fresh extract of the scratch board, with a connection-level verify by
   the oracle (not `rip_net`), and adds the result to `ctx.state` as `conn = -1`, `stage = 'legacy'` pieces.
6. **Plan fields before W9**: `plan.global.engine` / `stages` are not in `GLOBAL_FIELDS` yet. `engine.split_plan(plan)`
   strips the fields `plan.py` does not know from `global` before `plan.resolve` and returns them; `session.route`
   dispatches on that (or on `cfg.engine` once W9 puts it there). The engine reads stages from `x.stages`, else
   `cfg.stages`, else the split-off value.

## W9 (planx, diag, plan.py / route_tools.py hooks) — as built

Files: `v2/planx.py`, `v2/diag.py`, hooks in `guided/plan.py` and `tools/route_tools.py`; `pcb/route.py` read, not
changed (its `_finish_guided` builds a plan of v1 fields only — `global.layers / time_budget_s / ripup_rounds /
via_cost`, `nets[..].skip` — and hands it to `guided.route` unchanged, so it follows whatever engine `plan.resolve`
defaults to). Tests: `tests/test_v2_planx.py`, `tests/test_v2_diag.py`.

**Where the plan lands (W2, every stage)**

1. `guided.plan.resolve(board, plan)` now returns a `Config` with `cfg.engine` (`'v2'` default since 2026-11-11 | `'guided'`),
   `cfg.stages` (canonical-order tuple, always containing `'classify'`) and `cfg.x` = `planx.V2Cfg`. `engine` /
   `stages` and every other v2 field are in `GLOBAL_FIELDS / NET_FIELDS / REGION_FIELDS / PLAN_FIELDS` (added by
   `planx.extend_schema`, called once at the bottom of `plan.py`), so `engine.split_plan` is no longer needed —
   harmless if kept. `planx.DEFAULT_ENGINE` is the one constant; it is `"v2"` (see "v2 is the default" at the end).
2. v2 fields are validated for **both** engines; under `'guided'` they are ignored and `cfg.warnings` gets one line
   naming them. A region may now consist of `capacity_scale` / `no_vias` alone (v1 sees cost 1.0, no forbid).
3. `V2Cfg` as built (design §9 only said "resolved v2 hints + auto{}"):
   `engine, stages, tile_mm (None = auto), negotiation_iters (30), power_width_factor (2.0), fanout ('auto'|'none'),
   polish (True)`; `nets: {name: NetX}` only for nets with a hint — use `x.net(name)` (never raises; defaults) with
   `NetX.role | None, protect, via_budget | None, waypoints ((x, y, layer index | None), ...), between
   (('U3.4','U3.5'), ...), side_of (('U3','cw'|'ccw'), ...), tree | None`; `buses: [{'nets' (sorted), 'order'
   (lateral order), 'layer' (index | None), 'spacing' (centre-to-centre mm | None)}]` + `x.bus_of(net)`;
   `fanout_parts: {ref: {'style', 'side' ('n'|'e'|'s'|'w'|None), 'layer' (index | None)}}` + `x.fanout_for(ref)`
   (style `'none'` when `global.fanout == 'none'` and the part has no entry); `reserve: [{'at': (x, y), 'net'}]`;
   `regions: [RegionX(idx, rect, layers, nets, exc, name, capacity_scale, no_vias)]` + `x.capacity_scale(x, y,
   layer=None, net=None)` (product) and `x.no_vias(x, y, net=None)`; `given` (which v2 fields the plan set),
   `warnings`, `auto`, `overlays` (see 8), `x.has(stage)`, `x.last_stage`, `x.as_dict()`.
4. Decisions the design left open: `fanout.side` is a board direction `n|e|s|w` (y grows downwards; aliases
   north/up/…); `buses[].spacing` is centre-to-centre; `via_budget > max_vias` is clamped with a warning; `role:
   'pair'` needs a declared/auto diff pair and `role: 'plane'` needs a pour (both `BAD_PLAN` with the fix);
   `waypoints` must be on the board and on a layer the net may use; glob keys work for `nets`, `buses[].nets`,
   `fanout` refs.
5. Additive helpers: `planx.apply_clearances(oracle, board, cfg)` (W0 note 8 in one call),
   `planx.merge_patch(plan, patch)` (how a `suggest[]` patch is applied: objects merge, `regions / buses / reserve /
   diff_pairs` append, the rest replaces; `{'move': ...}` raises), `planx.auto_preview(board, cfg)` (what
   `pcb_route_plan` returns as `auto`; uses `classify.roles / detect_buses` when classify is built, else the §4.3
   rules), `planx.pad_minor(pad)`.

**What diag reads (W1, W3, W4, W7)** — all optional, diag degrades instead of raising:

6. `fail` dicts passed to `diag.failure(ctx, conn, fail)`: `reason`, `where (x, y)`, `blocked_by` (net names,
   `Blocker`s, piece ids or `(any of those, count)` pairs — ranked into net names), `static_share`, `tried`, `probe`
   (`'a'|'b'|'none'`, or `{'enclosed': bool, 'end': 'a'|'b', 'reached': n}`, or `{'a': {...}, 'b': {...}}`),
   `crossed {conn idx: cost}` or `mincut_nets [...]`, `cut` (`Cut` or dict), `frontier` (`[(x, y), ...]` or
   `{'a': [...], 'b': [...]}`, drawn in the failure crop). Missing pieces are derived: `static_share` by an
   8-direction oracle probe at `where`, `cut` from the pad-pair gap of the enclosed terminal (`tiles.pad_cut`, else
   `oracle.pad_gap` + the §4.6 formula) or the worst border of the connection's corridor.
   The entry has v1's keys plus `conn` (index), `cut`, `static_share`, `mincut_nets`, `probe`, `tried`, and the
   private `_a`, `_b`, `_frontier` (popped by the tool). It is cached in `ctx.history['diag_failed'][idx]`.
7. Tiles / global: §9 has no accessor for the tile origin, per-border demand or the planned paths. diag uses, in this
   order: `tiles.x0 / y0` (else a probe through `tile_of`); `ctx.history['global'] = {'path': {idx: [(tx, ty)]},
   'layer': {idx: [layer per tile]}}` as `globalroute.py` publishes it (or `StageReport('global').data['paths']`);
   `tiles.pad_edges(pad)` and `tiles.own_shadow(code, pos, net_id, half)` for crossings that take no free room;
   `tiles._reservations_booked` (protected / power / fanout pieces are then already off the capacity);
   `tiles.useE / useN` (the negotiated demand, used for a border that has no copper yet); `tiles.cut_for_edge`,
   `tiles.pad_cut`. Measured on the demo board after a stage-limited run: diag's demand equals `tiles.use*` on all
   295 borders with demand. Side `'N'` = the border towards `ty + 1`, as in `tiles.py`.
   Semantics chosen after the first end-to-end run (the naive "copper demand > tile capacity" reported ~10 false cuts
   on a fully routed, DRC-clean board): **copper that is there proves room for itself** (capacity is never below the
   routed copper on a border), and a tile border is a *cut* only where a connection that is still unrouted is
   planned through it without room. `StageReport('global').data['cuts']` is dropped once every connection is routed.
8. Pictures: the engine does not have to do anything. `diag.suggest()` — the last diag call of a run — leaves
   `diag.overlay_set(ctx, failed)` on `ctx.x.overlays`; `route_tools` reads `out['_overlays']` if the engine sets
   it, else `cfg.x.overlays` (`_cfg` is the same object the engine routed with), and renders with `diag.render`.
   Crops are made only for failures with a certificate that were actually tried (not "no search stage ran" /
   "before this connection was tried"), at most 6; the sketch only when no copper was routed; the congestion
   picture only when there are cuts or failures. `diag.build(ctx, fails=None, reference=None)` returns every §6.2
   key in one call for an engine that prefers that.
9. `diag.critique(ctx, reference)`: `reference` = `{net: [length_mm, vias]}` (or `{'nets': {...}}`), e.g. the human
   routing; then vias are compared per net and a detour is `> 1.5 x` the reference length. Without it:
   `vias_over_budget` needs `nets[..].via_budget`. `bus_unbundled` reads `x.buses` and `x.auto['buses']` (a dict
   with `nets` and optionally a list `order`; classify's `order: 'same'` string is ignored).

**Tools**

10. `pcb_route_plan` returns `auto` (`{roles (signal nets left out), buses, widths, layer_dirs, source}`). `analyze()`
    does not hand out its board, so the tool extracts a second time (`session.load_board`, read-only, ~1 s); if
    `guided.analyze` ever returns `_board` the tool uses it. With `planx.DEFAULT_ENGINE == 'v2'` it also runs
    classify + global and returns `cuts` (dormant while the default is `'guided'`: the tool has no plan argument,
    and `mcp/catalogue.py` pins its parameter list).
11. `pcb_route_guided`: `_findings` adds `CUT_OVER_CAPACITY` (per failed entry whose `cut` is over capacity, max 3,
    with the matching `suggest` patch as the fix), `ROUTE_QUALITY` (one per critique kind) and `SUGGESTED_PATCHES`;
    the `UNROUTED` fix leads with `suggest[0]`. `congestion.layers` is dropped from the tool result when nothing
    failed, nothing is over capacity and it is not a dry run (the summary `max` / `hot` stays).
    Not done (not W9's files): `mcp/catalogue.py`'s slim description of the two tools and `mcp/compact.py`'s
    `_shape_guided` do not know the v2 keys.

## W8 (polish.py) — S6

Files: `v2/polish.py`, `tests/test_v2_polish.py`, `bench/route/polish_bench.py` (polish on already routed boards,
with kicad-cli DRC before / after). Design §4.10 and the polish lines of §9 are updated in place.

**Interface as built** (§9 signature unchanged):

```python
PASSES = ("entry", "tight", "stairs", "vias", "widen")
def run(ctx, budget, passes=PASSES) -> StageReport     # unknown pass name -> ValueError
def polylines(state, conn) -> list[list[Piece]]        # runs in path order, broken at vias / junctions / open ends
CLEANUP_SLACK_MM = 0.0                                 # module default; ctx.x.polish_slack_mm overrides (see 2)
```

`StageReport.data`: `edits {pass: n}`, `edits_total`, `tried`, `vias_removed`, `widened_mm`, `deferred`,
`rejects {acute|score|illegal|soft|topology|connectivity: n}`, `slack_mm`, `loose`, `oracle_checks`,
`delta {necked_mm, vias, length_mm, bends}`, `score_before`, `score_after`.

**Deviations from the design, and things other work packages should know**

1. **Via removal uses patterns only, no window search** (§4.10 pass 4 says "pattern/window search"). The moved span
   is tried as: straight, the two L postures, the two Z postures, then the same polyline on the entry layer.
   `detail.route_conn` routes a whole connection, not a sub-span between two points, so there was nothing to call.
   Also handled: a single via in front of a through-hole pad. **W4**: a `detail.route_span(ctx, conn, a, b, layer,
   budget) -> polyline | None` would let polish remove more vias (measured on the 14 v1-routed benchmark boards: 29 of 1 210 vias removed).
2. **"Score never worse" and odd-angle clean-up conflict** (§4.10 first sentence vs the odd-angle / short-segment
   targets of §1). Replacing an odd-angle stub by a 45-degree posture is often *longer* (a straight line is the
   shortest path), and `Score` has no term for odd angles, so a strict per-edit rule refuses those edits. As built:
   a *clean-up* edit (fewer odd-angle / < 0.2 mm segments and no new ones, or a track end moved onto its pad
   centre) may cost length as long as the **stage's cumulative** Score delta is not worse; refused ones are retried
   once after the other passes. So `state.score()` after `run()` is never worse than before (what the engine's
   keep-best gate tests), but a single clean-up edit can be worse than the state just before it.
   When the stage has saved nothing, clean-ups that cost length are not made and are counted in
   `data["deferred"]` (+ a note). **W2 / W9**: if the odd-angle targets matter more than a few tenths of a
   millimetre, set `ctx.x.polish_slack_mm` (allowed growth of total length for clean-ups) and let the engine's gate
   for the polish stage tolerate `length_mm` growing by that much. Default 0 = the design's rule. On the 14
   v1-routed benchmark boards the default already brings odd angles from 19–39 % to 0–3.9 % (segments < 0.2 mm: 19–40 % to 2–21 %) because pull-tight
   earns more length than the clean-ups cost.
   Clean-up that is paid for by removed vias / re-widened length (lexicographically "better") is capped at
   +1 % of the routed length (`GROW_CAP`).
3. **Added to every geometry pass: tidy.** Dead ends and loops that lead nowhere are removed, collinear neighbours
   are merged (not in §4.10; both only ever improve the Score).
4. **`RouteState.connected(conn, loose=True)`** (W1's addition) is used when present: a connection that is only
   joined by KiCad's rule (an end inside other copper, legacy output) is polished and re-checked with `loose=True`;
   on a net that holds such a connection every end of another connection lying *inside* this connection's copper
   counts as attached (the segment is frozen / the vertex anchored), not only ends on the centre line.
5. **What polish never touches**: pieces with `protected=True` (and the segments they join stay put), connections
   with `role == "pair"` (moving one half breaks the coupling), connections that are not `routed` + connected,
   pieces with `conn == -1`, segments another piece tees onto, vertices where anything else of the net attaches
   (other connections' ends, pad centres, terminals, kept copper, same-net pads that are not the connection's own
   terminals, pour islands). It reads `cfg.nets[net].layers_ok` (via removal) and forbidden `cfg.regions`.
6. **Thermal reliefs and pours are only as safe as the soft shapes.** Polish refuses an edit that crosses a
   `thermal` / `reserve` soft shape the old copper did not cross, but it has no model of zone fills. Measured on
   v1-routed boards *without* S1's spoke shapes (bench/route/polish_bench.py --drc, 14 boards): 0 new clearance /
   short / hole / edge errors, but 3 boards got one new `starved_thermal` each and on `one_air_max` one GND pour
   island was cut off (unconnected 43 -> 44). **W5**: spoke corridors must be in the oracle before polish runs;
   **W2**: the judge must run after polish (it does in the design) and blame polish pieces like any others.
7. **Budget charges** (not in §3): `CHARGE_BUILD = 60` per connection view, `CHARGE_TRY = 6` per candidate that
   reaches the oracle, `CHARGE_EDIT = 60` per transaction, plus 1 per 8 oracle checks. Measured 85–260 k
   units/s on the corpus with the real `RouteState`, i.e. about the 200 000 units/s the budget scale assumes.
8. **Score noise**: polish compares scores at 1 nm. W1's `RouteState.score()` sums integer nanometres, so a neutral
   edit (collinear merge) does not change it; a state implementation that sums floats would need rounding before
   the engine's strict comparison.
9. **Pour-island terminals**: polish treats a vertex inside a same-net pour island (`board.clusters[net][cid]
   ["islands"]`) as attached. Islands that touch no pad are not in the model (`_island_cluster` returns 0); a
   track that ends in such an island is only kept because removing it would disconnect its own connection.

## W2 (judge, engine, session hook) — as built

Supersedes the W2 "assumptions" section above where they differ (W1's `kio` / `pieces` / `legacy` and W9's `planx`
landed while W2 was being written; W2 adapted to them, nobody has to change anything).

**`judge.py`** (tests: `tests/test_v2_judge.py`)

1. `run(ctx, budget, pr, scratch, baseline_raw, reroute) -> StageReport` — §9 signature. On return `ctx.state` and
   `scratch.pcb` are the best round. `pours_pass(ctx, pr, scratch, budget, connect=None, reroute=None, dogbones=None)`
   — three **additive keywords**: `connect(board2, budget) -> (piece ids, failed dicts)` is the engine's pours search
   (the judge itself imports no search), `reroute` lets the pours pass give a crowded pour pad room,
   `dogbones=False` keeps `power.place_dogbone` out when the plan's stages leave `power` out.
2. **Scratch board**: `kio.commit_and_check(fresh=True)` resets the scratch board from `pr.pcb`. The judge keeps its
   own *base* file (`<scratch>.kicad_pcb.v2base`, the scratch board as handed in: rip-up applied, no v2 copper) and
   passes kio a stand-in project whose `.pcb` is that file, so every round commits the whole state to the same board
   and the user's board file is never read by a round. `<scratch>.kicad_pcb.v2best` is the best round's file;
   `judge.cleanup(ctx)` deletes both.
3. **Score of a round**: `Score(new_errors = len(new markers), opens = KiCad's unconnected count over the targeted
   nets (ctx.history['targets']), rest from state.score())`. Opens come from KiCad, not from the state: a pour net's
   pads and W0's note 14 (a short that shows up as an open) are only visible there.
4. **Blame**: `kio.marker_pieces` for `session.COPPER_TYPES`; `starved_thermal` = this run's copper of other nets
   inside the pad's neighbourhood (v1's `_pad_rect`, then 1.5 mm wider); every other type is not ours (reported in
   `drc.new_top`). Two of our connections in one marker: the later one in `rank` order goes (pour stubs before
   signals, reservations last). `conn == -1` pieces are removed as a group (same-net loose pieces chained by end
   points).
5. **Escalation per (connection, rule)**: 1st blame → cost region ×3 (1 mm) for that net + history mark, re-route;
   2nd → forbid region (0.4 mm; cost ×8 when the net has a pad there), re-route; 3rd → removed, reported. Starved
   pad: neighbourhood closed (regions; spoke corridors hardened in the oracle when `power` is built) and the crowd
   re-routed, once for the close rectangle and once for the wide one, then the crowd is removed.
6. `ctx.history['judge']` = bookkeeping (`last` = the accepted check incl. kio's `ex`, `rounds`, `removed`,
   `protected`, `repairs`, `pour_failed`, `pour_pieces`, `unblamed`); `ctx.history['marks']` and regions named
   `"judge"` in `ctx.cfg.regions` as announced. **W4 / W7**: a search that honours plan regions already sees the
   judge's feedback; `marks` is extra.
7. Deviation from §4.11's pseudo-code, on purpose: the removal step after the rounds is repeated (≤ 3) and is itself
   under keep-best — when removing the blamed connections does not lower the error count, their copper is kept and the
   residual is reported (guarantee 3: nothing is removed that an exact check or marker does not justify).

**`engine.py`** (tests: `tests/test_v2_engine.py`)

8. `route(pr, cfg, x, ex, *, scratch, baseline_raw, budget_units, progress=None) -> dict` — §9 signature. `scratch`
   must already hold the board to route (rip-up applied and saved); the judged copper ends up in `scratch.pcb`, the
   engine never writes `pr.pcb`. Extra private keys for the session hook / tests: `_judged`, `_targets`, `_touched`,
   `_unconnected`, `_stats`, `_commit` (the commit list, for determinism tests), `_ctx`. Public additions: `engine`,
   `stages`, `auto`, `judge {rounds, commits}`, `not_committed` (plan-only run), plus diag's keys when it is built.
9. **Which S0**: W5's `classify.run` is used when `power` is selected *and* built (build stage 2), or when
   `x.classify == 'full'` / env `COPPER_V2_CLASSIFY=full`; otherwise S0 is build stage 1's
   (`legacy.plan_connections(board, cfg, 'signals')`). **Which detail**: S4 (`detail.run`) when built, with `legacy`
   as fallback per connection; `x.detail == 'legacy'` / env `COPPER_V2_DETAIL=legacy` selects build stage 1's search
   (then `repair` is not run: S5 needs S4's windows). So build stage 1 on today's tree is
   `plan.global.stages = ["classify", "detail", "judge"]` + `COPPER_V2_DETAIL=legacy`. **W9**: a plan field for the
   detail engine (`global.detail: "window" | "legacy"`) would replace the environment variable; the engine already
   reads `x.detail` and `x.classify`.
10. **Budget**: `Budget(budget_units)`; each stage gets `total.sub(STAGE_SHARE[name])`; the legacy slot gets
    `1 − Σ shares of the built reservation / repair / polish stages` (build stage 1: the whole budget, as v1's signal
    pass); judge `sub(0.05, floor=max(2 s, 5 %))`; pours `sub(0.2, floor=20 %)` (v1's rule: pour pads always get a
    slice). Floors can take a run over its budget, as §3 allows.
11. **Gates**: `power` / `fanout` / `global` — every piece the stage added must pass `oracle.piece_ok`, else the
    stage is undone; `detail` / `legacy` / `repair` / `polish` — undone when `state.score()` got worse. A stage that
    raises is undone and reported in `warnings` + its `stages[]` notes; `COPPER_V2_STRICT=1` re-raises (tests,
    benchmark). `ToolError` (KiCad I/O) always propagates.
12. **Build stage 1 only**: when the judge's local re-route leaves connections unrouted and the detail engine is
    `legacy` (which cannot move copper already in the state), the engine may route all connections again from
    scratch with the judge's regions in place (`_wide_reroute`): boards ≤ 250 connections, ≤ 2 times, only while the
    run stays under 1.5 × budget, kept only if `state.score()` improved. v1 does the same without the keep-best rule.
13. **Pours search** (`engine._Run.pour_connect`, handed to `judge.pours_pass`): `legacy.plan_connections(b2, cfg,
    'pours')` + `legacy.route_all` on a fresh `Ctx` for the refilled board, re-checked against the run's own oracle,
    added to the run's state as `conn = -1`, `stage = 'legacy'` pieces.

**`guided/session.py`** — one dispatch (`if cfg.engine == "v2": return _route_v2(...)`) after the plan is resolved,
the rip-up extract is taken and `compare`'s existing score is measured, plus the function `_route_v2`: scratch copy,
rip-up + fill saved to it, baseline DRC, `engine.route`, then the write: checkpoint, one atomic replace of `pr.pcb` by
the judged scratch board, final stats / extract / `boardmod.drc`. `dry_run` / `compare` / "rip-up bought nothing" /
`previous` behave as in v1; v1's code path is untouched.

## W1 (pieces, kio, legacy, bench adapter) — as built

Files: `v2/pieces.py`, `v2/kio.py`, `v2/legacy.py`, `bench/route/routers/v2.py`, `bench/route/legacy_probe.py` (W1-only
diagnostic), `tests/test_v2_pieces.py`, `tests/test_v2_kio.py`, `tests/test_v2_legacy.py`. Nothing in §9 was renamed or
removed; everything below is additive or fills a point §9 leaves open. No v1 file was edited.

**`pieces.RouteState` (§4.2, §9)**

1. `rip_conn(conn, *, protected=False)` — **protected pieces stay by default** (escape stubs, dog-bones: a connection
   whose `Terminal.escape` points at its stub must not lose the stub when it is ripped for a re-route). Pass
   `protected=True` to take them too. The connection goes back to `'todo'`. §9 has no flag.
2. `connected(conn, loose=False)`. Strict (default) is the design's rule: an end within `pieces.TEE_TOL` = 1.5 nm of
   the other piece's centre line (1 nm + the 1 nm quantisation of both items), or inside static copper of a cluster
   (pad shape, kept track / via, pour island). `loose=True` also counts an end that merely lies inside the other
   piece's copper — KiCad's own rule, and what v1-made copper needs (paths may end off-centre inside a track).
   Strict implies KiCad-connected. A segment that passes through a pad without ending in it is not a join.
3. **Open terminal** (v1's pad-to-pour connection, "anything else of this net"): `Terminal(x, y, layers=())` with
   `pad=None`, `cluster=0`; test with `pieces.is_open(t)`. `connected()` of such a connection = terminal a's group holds
   another static cluster of the net. `x, y` are only an aim (the nearest other pad).
4. Added: `fail(conn)` (the record given to `set_status(conn, 'failed', fail)`), `piece(pid)`, `conn(idx)`, `depth`
   (open transactions), `len(state)`, `net_groups(net, loose)` → `[(cluster ids, piece ids)]`, `dangling(net, loose)` →
   'routed' connections that are no longer connected (after a rip of copper they teed onto: rip them too or re-route),
   `export(only=piece ids)`.
5. `add(**fields)` takes the `Piece` fields without `id`; unknown fields raise `TypeError`; a via needs only `x1, y1`
   (`layer` is forced to −1); coordinates / sizes are quantised with `q()`. `add` does **not** check legality.
   Ids are monotone and never reused; rollback / restore bring a piece back with its id.
6. `new_errors` is a property (journaled, part of snapshots). `commit(tok)` of an outer token closes inner ones;
   a closed or unknown token raises `ValueError`. Outside a transaction nothing is logged.
7. `export()` → `(tracks, vias, ids)` with `ids` = track ids then via ids, i.e. aligned with `track_uuids + via_uuids`
   of the commit (= `commit_and_check()["uuids"]`). Segments shorter than 1 nm are not exported (and have no id in
   the list). Coordinates are the 1 nm values the oracle checked (v1 wrote 0.1 um-rounded ones).
8. `score()`: `opens` = `len(conns)` − connections with status `'routed'` (the conns list is held by reference, so
   classify may fill it after the state exists); `necked_mm` = segments with `w < target_w`; `bends` = points where
   exactly two segments of one connection meet at an angle. Lengths are summed in integer nm: the score of a state
   does not depend on its add / remove history (tested).

**`kio` (§9)**

9. `commit_and_check(pr, scratch, tracks, vias, baseline_raw, *, ripup="none", rip_nets=None, fresh=True, remove=None,
   drc=True)`. **`fresh=True` (default) first copies `pr.pcb` over `scratch.pcb`** (then applies `ripup`), so one call
   = "the board plus exactly this copper"; hand it an object whose `.pcb` is your base file if the base is not the
   project's board (W2's judge does). `fresh=False` commits into the scratch board as it is on disk (`remove=[uuid]`
   deletes first) — for incremental passes; `pr` may then be `None`. Extra result keys: `track_uuids`, `via_uuids`,
   `ex` (extract of the committed, refilled board).
10. Marker format of `new` (and of `kio.new_markers(raw, baseline_raw)`): the four keys of `session._new_errors`
    (`type`, `message`, `items` = first two item texts, `at`) plus `uuids` (every item), `pos` (`[[x, y], ...]`),
    `copper` (type in `session.COPPER_TYPES`). Sorted by (type, x, y, message). `marker_pieces(marker, uuids,
    piece_ids, state, radius=0.3)` also accepts a raw kicad-cli violation; uuid hits win, and a blamed uuid whose
    piece has left the state falls back to proximity.
11. Added: `new_markers`, `baseline_drc(pr, ripup, nets)`, `scratch(pr)`, `cleanup(sc)`; `extract()` adds
    `ex["_ripped"]`.

**`legacy` (§9)**

12. `route_all` does more than "override `_verify`", because the measured failure (A1) is cheaper to prevent than to
    repair: (a) every path is checked by the oracle **when it is found**; a rejected piece re-rasterises the violated
    shapes into this run's occupancy map with the oracle's need + `raster_guard(grid, reach)` and the search runs
    again (≤ 3 times; `stats.emit_rejects / emit_restamps / emit_blocked_cells`); (b) v1's `Board.seg_clear`
    (rule + 2 um) is answered by `Oracle.seg_legal` for escape stubs and end snaps; (c) the negotiation rips the
    **connections** a soft path crosses, not their nets (`legacy.RIP_LEVEL = "net"` restores v1's behaviour for A/B),
    plus every connection that teed onto ripped copper (`stats.dependent_rips`); (d) the final `_verify` rips the
    one failing connection, re-queues it once, then reports it with v1's phrase (`legacy.SELF_CHECK`).
    On a board where none of this fires the copper equals v1's (tested on synthetic boards, to 0.1 um).
13. `legacy.plan_connections(board, cfg, mode="signals"|"pours"|"all", start=0)` → v1's plan as `ConnV2`
    (`rank` = v1's key, pour connections with an open terminal b, `role` 'plane' / 'signal'); `legacy.skipped(board,
    cfg)` → v1's `skipped[]`. 'pours' needs a board extracted **after** the zones were refilled around the new copper
    (islands), i.e. a second `Board` + `Oracle` + `RouteState` — one `RouteState` cannot span both, its oracle is
    bound to one board.
14. Inputs `route_all` relies on: `ctx.cfg.step` (it makes `board.grid` itself and **clears `board.grid.classes` on
    every call**; the static rasters are cached on the grid object as `grid._v2_static`); terminals that resolve to a
    pad cluster of the net (`Terminal.cluster`, else the pad name) — a bare point terminal is reported as failed, two
    terminals in one static cluster are 'routed' without copper. Order: `ConnV2.rank` when every connection has one,
    else v1's key. Pieces already in the state are fixed: never ripped, obstacles for other nets, **targets for
    their own net only when their group touches a pad cluster** (floating copper is not a target).
15. Output: pieces `stage='legacy'`, `target_w = max(ConnV2.width or plan width, w)` (a necked result has
    `w < target_w`); status + `state.fail(idx)`; `report.data` = `failed` (v1 format + `conn`), `stats`, `pass`
    (a v1 `passes[]` entry), `clock_hit`, `not_connected` (routed by v1 but not `connected(loose=True)`; empty in the signal
    pass of all 14 corpus boards). The call is one transaction (an exception rolls the state back). `budget.left()` is v1's work
    limit; charged = v1 work + oracle checks / 8. v1's wall-clock safety net is still inside `Router.spent()`
    (`clock_hit` is reported in `report.data` and as a note).
16. Plan `regions` appended to `ctx.cfg.regions` between calls (W2's judge feedback) are honoured: v1's cost field is
    rebuilt per call. `ctx.history` is not read by legacy.

**Findings for other packages (measured with `bench/route/legacy_probe.py`, KiCad 10)**

17. **W0 / W2 — oracle false-legal, hole clearance with a pad override**: on `cm5_minima` KiCad reports
    `hole_clearance` "(pad clearance 1.7000 mm; actual 0.9741 mm)" for two +5V tracks next to an NPTH pad of
    `Module301` at (120.80, 32.00) and (127.17, 32.00). KiCad applies the pad's / footprint's local clearance to the
    hole test; `Oracle._need` uses the board hole clearance for `cls == 'hole'` and so accepts them. These are the
    same 2 new errors the v1 baseline has on that board. Until the oracle models it the judge has to remove them.
18. **W2 / W5 — `starved_thermal`** is the only other new-error type the legacy stage produced without a judge
    (10 markers: `interf_u` 8, `multichannel_mixer` 1, `synth_qfp_fanout` 1). Against the signal pass's pieces
    `marker_pieces` returned `[]` for every one of them (no uuid of ours in the marker, no piece within 0.3 mm of a
    marker position; the probe did not test the pours-pass copper): blame for this type needs the pad's
    neighbourhood (v1 used `_pad_rect` + `_intruders`), not the 0.3 mm radius.
19. **Budget**: `tinytapeout` (183 of 349 signal connections) and `interf_u` (20 of 159) run out of budget in the
    legacy stage at the default 90 s; that, not verify, is what is left there for stages 3+.

## W6 (flow, fanout) — as built

Files: `v2/flow.py`, `v2/fanout.py`; tests `tests/test_v2_flow.py`, `tests/test_v2_fanout.py`. Nothing outside these
four files was edited. The §9 signatures are kept; everything below is either additive or a point §4.5 / §9 left open.

**flow.py**

1. `min_cost_flow(n, edges, s, t, want) -> (flow, cost, flow per edge)` as in §9. Costs are converted once to integers
   of 1e-9 (`flow.COST_SCALE`) and all path arithmetic is integer: no float drift, ties are real ties and are broken by
   structure (heap order (distance, node number); a node keeps the first arc that reached its final distance). Negative
   costs allowed (one Bellman-Ford pass); `ValueError` for a bad node / capacity / cost or a negative cycle. `flow <
   want` is the "infeasible" answer (maximum flow, at minimum cost for that amount). Additive: `flow.assignment(costs,
   capacity=None) -> (column per row | -1, cost)`.

**fanout.py — what other packages must know**

2. **Escape pieces are `conn = -1`, `stage = 'fanout'`, `protected = True`, `target_w` = the full width** (`w` may be
   the neck). They belong to the pad, not to one connection: a pad can end several connections (tree targets), and
   ripping one must not pull the stub from under the others. `RouteState.rip_conn` leaves them anyway (protected).
3. **Escape terminals**: every connection ending on an escaped pad gets the *same* `Terminal(x, y, layers, pad=<pad
   name>, cluster=<the pad's>, escape=True)` at the stub end. Ring escapes: `layers = (pad layer,)`. Via escapes: the
   via centre, `layers` = every copper layer (or `(plan layer,)` when `plan.fanout[ref].layer` is set). The stub's last
   piece ends exactly on the terminal point (seg end or via centre), so `RouteState.connected` finds it as a bare point.
4. **The book** — `fanout.registry(ctx)` = `{'by_pad': {key: record}, 'hist': {key: [records, oldest first]},
   'boxed': {key: fail}, 'normals': {...}}`, `key = (pad name, round(x, 4), round(y, 4))`; record = `{key, pad, ref, net,
   kind: 'ring'|'via', pieces: [ids], terminal, orig: {(conn idx, 'a'|'b'): pad Terminal}, desc, width, full_width, clr,
   layer, lane, length, soft, via}`. It lives in **`ctx.escapes`** (set dynamically: `types.Ctx` has no such field —
   W0: please add `escapes: dict | None = None`). `fanout.escape_of(ctx, 'U3.4'[, x, y])` returns the current record.
5. **Keeping terminals and stubs together** — `fanout.sync(ctx)` re-aligns the book with the state: for every pad that
   ever had an escape the current record becomes the newest one whose pieces are all in the oracle (one dict look-up
   per escape piece), else the pad's connections get their pad terminals back. Call it after anything that adds /
   removes fan-out pieces behind fanout's back:
   - `RouteState.restore(snapshot)`: **`fanout.run` wraps `ctx.state.restore` on that instance** (`fanout.hook_state`)
     so the engine's keep-best gate and the judge need no change. `fanout.HOOK_STATE = False` turns the wrapper off
     (then call `sync` yourself). W1/W2: an official `RouteState` change hook would be cleaner — say so and I drop the
     wrapper.
   - `RouteState.rollback` is **not** hooked: whoever rolls back across `re_escape` calls `fanout.sync(ctx)` (repair
     does, `_fan_sync`).
   - **W2 (judge)**: when a marker blames a fan-out piece and its loose group is removed, call `fanout.sync(ctx)` and
     set the users (`record['orig']` keys, read them *before* removing) back to `'todo'`: their routes start at a stub
     end that no longer exists. Without that the connections stay "routed" and open. Not hooked, not tested end to end.
   - `fanout.undo(ctx)` removes every escape. `run()` undoes itself when it raises.
6. **Return values** — `ring_fan(ctx, ref, row, style='auto')` and `via_escape(ctx, ref, pads)` take Pad objects, pad
   names (`'U1.3'`) or pad numbers and return one dict per pad, in input order:
   `{'pad', 'ok': True, 'kind': 'ring'|'via', 'pieces': [Piece], 'terminal', 'desc', 'width'}` |
   `{'pad', 'ok': False, 'fail': {'reason', 'blocked_by': [tag], 'where': [x, y]}}` |
   `{'pad', 'ok': False, 'skip': '<why nothing was to do>'}`. Ring `desc` = `{ring: 1..3, from: 1..3, dir: -1|0|1}`;
   via `desc` = `{dir: 'out'|'in'|'dogbone'|'d<i>', depth, shift}`. Both place the copper and move the terminals.
7. **`re_escape(ctx, conn, end, avoid_side) -> bool`** — `avoid_side`: `'n'|'e'|'s'|'w'` (repair passes the compass
   side the stub leaves on), `'left'|'right'` (-t / +t of the row), `'out'|'in'`, or `''`. It is a *preference*: when
   no legal escape avoids that side, any escape other than the current one is taken (a straight stub and its legs all
   head the row's own side, so a strict reading would leave only inward vias). Refuses (False, nothing changed) while
   *another* connection is routed onto the stub; the caller rips first. Works for a pad that never had an escape.
8. **Boxed in** — a pad is reported `pad boxed in` (and its `'todo'` connections set `'failed'` at once) only when
   every ray from 5 points along the pad's long axis in 8 directions, at the neck width, is illegal **against static
   copper alone** (dynamic copper can still be ripped). Otherwise it is "unescaped": left to detail, listed in
   `StageReport.data['unescaped']`, status untouched. The fail record: `{reason: 'pad boxed in', where: [x, y],
   blocked_by: [net names], blockers: [tags], static_share, tried: ['fanout'], cut: Cut, pad, net, probe: {enclosed:
   True, end: 'a'|'b'}}`. Its `Cut`: on a pad row, the gap between the pad's **two neighbours** (the stub has to leave
   between them) when that gap carries no track of the neck width — `tiles.pad_cut` when `ctx.tiles` exists (its
   `demand_mm` / `nets` are filled in here), else `oracle.pad_gap` + the §4.6 formula; otherwise `Cut(a=pad, b=<tag of
   the nearest blocker>, capacity_mm=0)`. `StageReport.data` = `{parts: {ref: {escaped, ring, via, unescaped, boxed}},
   escapes, unescaped, boxed, cuts: [Cut], critique: [{kind: 'bga_escape', ref, value, ...}], failed: [v1-format
   entries of the connections failed here]}`; `routed` = escapes placed, `failed` = boxed pads.
9. **Plan** — read from `ctx.x.fanout` (`'auto'|'none'`) and `ctx.x.fanout_parts[ref]` (`style`, `side`, `layer`) as
   `planx` built them. A per-part entry wins over `global.fanout: 'none'`. `style`: `out` = ring only; `in` = inward
   vias only; `via` = vias (either side); `auto` (below). `side` (`n|e|s|w`): the exit side of a single-row part (no
   outward side of its own), and the direction legs lean to when the row runs along it. `layer`: the layer the escape
   must end on — another layer than the pad's means a via, with the terminal on that layer only; for a through-hole
   row it is the stub's layer.

**Decisions where §4.5 was open or did not survive contact** (each has a test):

10. *Rows*: >= 3 pads of the same extent along the line with collinear centres, in the footprint's own axes
    (`fanout.footprint_rows(board, ref) -> [Row]`, cached on the board). The exposed pad of a QFN, mounting pads and
    2-pad parts are in no row; `needs_fanout` is False for a footprint without a row (the §4.5 formula alone would
    fan out every 0402). A pad matrix counts as a *grid* (BGA: dog-bones only) only when its two pitches are within
    1.5x of each other; otherwise it is a multi-row connector and the row is the denser line. An inner row of such a
    connector exits towards the side where the next parallel row is further away.
11. *Through-hole rows are not fanned out* unless the plan names the part (a 2.54 mm header passes the §4.5 test, but
    its pads reach every layer from every side: a stub only takes freedom away).
12. *Widths*: full width, then `conn.neck`; nothing narrower (plan `neck: false` is respected). A fine-pitch pad that
    cannot be left at the neck width against static copper is boxed in (tinytapeout U6 needs classify's
    `neck = min(class, Default)`: at the 0.245 mm class width every pad of that QFN is boxed, at 0.2 mm none).
13. *Ring geometry*: rings at `pad_len / 2 + k x rs` from the row line, `rs = w + cl` of the row's **most common**
    track (one wide power pin must not push every slot out), both snapped up to the window lattice
    (`fanout.lattice_pitch`, §4.7 rules a and b), so stub ends are whole lattice steps from the pad centre. Candidates
    are a superset of §4.5: stub straight to ring `a`, then a 45 degree leg to ring `b > a` (the design only has
    `a = 1`); `a = 2` is what lets the second pad from a row end lean as well.
14. *Planarity* is a rule, not only an outcome: slot order = pad order, and no escape may close the outward lane of
    another connected pad of the row — including pads that got no escape (their exit is kept at level 0). With all
    pads connected, legs therefore only appear where a lane is really free (row ends, gaps, pitch >= sqrt(2) (w + cl)).
    `fanout.LEAN_TO_PARTNER = False` turns the optional leaning off (straight stubs, legs only for pads without a
    straight exit).
15. *Via escapes never box a neighbour in*: an outward via closes the outward lanes within `via / 2 + cl + w / 2`. It
    is not offered where that would hit a pad outside the batch, and when a pad of the batch ends without a via the
    batch vias in front of it are taken back (they fail with "via would close a neighbour's exit" and get the ring).
    Measured consequence: a 0.5 mm QFN with an exposed pad and 0.6 mm vias gets ring stubs, not a via under every
    other pad with the pads in between walled in. Order per row in `auto`: vias for pads whose every partner is on
    another layer -> ring for the rest and for whoever got no via -> vias for whoever the ring could not serve.
16. *The flow proposes, the oracle disposes*: network = pads -> via columns (bins of `(via + cl) / sqrt(2)`, capacity
    1: two sites in one bin always conflict) -> pad-pair cuts (capacity = tracks, only for stubs that pass between two
    neighbouring pads) -> sink; cost = stub length + congestion (`tiles.via_cap`, 0 without tiles) + 6 x (w + cl) per
    thermal spoke crossed. Pairwise conflicts a flow cannot express (sites in neighbouring bins, a stub next to a
    neighbour's via) are found when the proposal is realised: the option is dropped and the flow re-solved for the
    pads still open; a pad left stranded gets one augmenting swap (move <= 2 blocking escapes to another of their own
    candidates, judged on oracle-only stand-in pieces with negative ids before the state is touched).
17. *Not escaped on purpose*: `role == 'plane'` connections (S1 owns them), connections that are not `'todo'`, pads
    that already carry copper of their net, pads whose partner is nearer than ring 3 (the pattern router is better;
    their exit lane is still kept free). Soft shapes: a `reserve` of another net is never crossed; a `thermal` spoke
    is crossed only when no clean candidate exists.
18. *Tiles are optional* (S2 runs before S3): `ctx.tiles is None` is fine. With tiles: `via_cap` prices via sites,
    `book_via` books placed vias, `pad_cut` gives the cut capacities.
19. *Budget*: 200 units per pad per attempt (`CHARGE_PATTERN`) + 1 per 8 oracle checks, tested before every row; a
    spent budget leaves the remaining rows to the detail stage and says so.

**Findings for other packages**

20. **W0 (oracle)** — one rule gap found by committing the escapes of all 14 corpus boards and running kicad-cli DRC
    (765 escapes; 0 errors on fan-out copper except this one): KiCad applies the **clearance override of an NPTH pad
    to its hole** — `cm5_minima`: "Hole clearance violation (pad clearance 1.7000 mm; actual 1.6195 mm)", track vs
    "NPTH pad of Module302". The oracle uses the board hole clearance for holes and ignores the pad's override there
    (false-legal). `tests/test_v2_fanout.py --full` counts it as a known gap (allows <= 2) instead of failing.
21. **W2 / integration** (measured while sibling files were still changing, so indicative only; every pair below
    ran concurrently on one code snapshot, harness numbers, 0 new DRC errors in all of them):
    `tinytapeout` 288/352 = 81.8 % with the fan-out stage vs 268/352 = 76.1 % with `global.fanout: 'none'` (baseline
    v1 38.1 %); `synth_qfp_fanout` 63/64 = 98.4 % (two runs, identical copper) vs 61/64 = 95.3 % without, and
    62/64 with `fanout.LEAN_TO_PARTNER = False` (baseline v1 93.8 %). An earlier pair on that board, minutes apart
    and across other packages' edits, read 89.1 % / 92.2 % with and 96.9 % without: the differences came from the
    judge (starved thermals at the headers' GND pins) and the pours pass, not from fan-out copper — but it shows the
    result on that board is not yet stable enough to call the stage-4 gate (100 %) either way.
22. **W9 (diag)** — `fail['probe']` is `{enclosed: True, end: 'a'|'b'}`; `blocked_by` holds net names and
    `blockers` the oracle tags (`'U6.15'`, `'track'`, ...). `StageReport('fanout').data['critique']` carries
    `bga_escape` entries for diag to merge.

## W7 (repair.py) — S5, as built

Files: `src/copper_kicad/pcb/v2/repair.py`, `tests/test_v2_repair.py`, `bench/route/repair_trace.py` (what the stage
did on one corpus board: rung outcomes, work, wall time split, certificates; `--set RESCUE_FIRST=0`, `--profile`).
Design §4.9 and the `repair.py` block of §9 carry an "as built" note; nothing of another package was edited.

**Public interface** (§9 names unchanged; additions marked +):

```python
def run(ctx, budget, only=None) -> StageReport            # data: repaired, failed, certificates {idx: cert}, stats,
                                                          #       log [what each rung did], tried {idx: [step]}, feedback_loops, tabu
def mincut_rip(ctx, conn, budget, tabu: set) -> bool      # tabu: (conn idx, frozenset(victim idxs [+ -1 for reservations]))
def permute_cluster(ctx, conns, budget) -> bool           # budget = the per-cluster cap; True = state improved
def rescue(ctx, conn, budget) -> bool
def rip_price(ctx, victim: int, piece: Piece | None = None) -> float      # + piece: its protected flag counts; reservations (victim < 0)
def probe(ctx, conn, budget=None) -> dict                 # + {enclosed: 'a'|'b'|'none', ends, static_share, blockers}
def certificate(ctx, conn, budget=None) -> dict           # + the failed[] record (also stored with set_status(..., 'failed', cert))
def memory(ctx) -> Memory                                 # + ctx.history['repair']: tabu, rip history, what each ladder found, log
DEPS: dict                                                # + {'detail'|'fanout'|'power'|'globalroute': module-like} override (tests)
```

Certificate keys: everything in the detail stage's fail record, plus / completed: `reason`, `where`, `cut`
(`{a, b, layer, capacity_mm, demand_mm, nets}` or None), `blocked_by` (ranked names: min-cut nets, then the detail
stage's, then the probe's), `blockers` (probe detail), `static_share`, `mincut_nets`, `probe {enclosed, ends}`,
`tried` (the detail stage's rungs followed by the ladder steps), `ladder` (the ladder steps alone: `route`, `probe`,
`rescue`, `mincut`, `permute`, `feedback`), `suggest [{why, patch}]` (patches use existing plan fields — `nets.priority`,
`nets.width`, `global.time_budget_s` — or `{"move": {"ref", "by"}}`).

**What repair relies on in the siblings** (read from the code that landed; the §9 text alone did not say):

1. **W4 `detail.route_conn`** — `soft_cost` is passed as a **callable `Piece -> mm | None`** (None = that piece stays
   hard); a soft result is taken as "a plan" whenever `crossed` is non-empty, and repair rolls back whatever a soft call
   added (W4 adds nothing). `crossed_pieces` is needed to rip protected copper: without it protected pieces are never
   offered to the soft search. The lifted layer assignment (point 6) relies on `window.allowed_layers` ignoring a
   corridor with empty `layers`. A failing `route_conn` costs up to 6 windows (>= 36 000 units): on `interf_u` 181 calls
   used the whole 3.96 M stage budget.
2. **W1 `RouteState`** — `fail(conn)` is used (falls back to `fails[conn]`); statuses, fail records, terminals and
   corridors of every touched connection are saved and put back by repair itself after a rollback, so a state that
   does not journal them works too. `connected(conn)` is called for every routed connection of a ripped victim's net
   (tee dependents are ripped and re-routed with it).
3. **W6 `fanout`** — stubs are `conn = -1`, `stage='fanout'`, `protected`. Repair finds the users of a crossed stub in
   `registry(ctx)['by_pad'][key]['pieces' | 'orig']`, rips every routed user, then calls
   `re_escape(ctx, user, end, avoid_side)` with `avoid_side` = the compass side (`n|e|s|w`) the present escape leaves
   its pad on, and calls `sync(ctx)` after every rollback that crossed a `re_escape`. A double with conn-owned stubs
   and no registry also works.
4. **W5 `power`** — a crossed dog-bone (`conn = -1`, `stage='power'`, `protected`) is removed and
   `place_dogbone(ctx, pad, exclude=frozenset(old via sites))` must return a replacement after the failed connection
   is routed, else the transaction rolls back. The pad comes from `ctx.history['power']['dogbones']` (else from the
   geometry); repair restores that dict after a rollback. **Gap**: a reserved soft site that `place_dogbone` consumed
   (`remove_soft`) is not re-created by the rollback (soft shapes are not in the state's journal).
5. **`ctx.history`** — repair keeps `ctx.history['repair']` (a `Memory`) and appends `(x, y, 0.5, 0.3, layer)` to
   `ctx.history['points']` (the list `window.build` prices) for the spots of a rip that did not pay. It does not read
   the judge's `marks`.
6. **W3 `Corridor`** — the "one extra via" rung and the second try of a victim's re-route replace the connection's
   corridor by `dataclasses.replace(cor, layers=(), layer_seq=())` (tiles kept), so the search may change layer where
   the global layer assignment did not plan it; the old corridor is put back when that route fails.
   `globalroute.run(ctx, budget, only=[...])` is called with the affected connections ripped (status `todo`);
   `tiles.book(...)` with `+(w + cl)` on the borders of the failure's tile that its corridor crosses, and with the
   negative amount when the loop is rolled back.
7. **`plan.protect`** — `ctx.x.net(name).protect` nets are never offered to the soft search, never put in a cluster,
   never re-globalled.

**Deviations from §4.9** (each measured, or forced by a sibling's contract):

8. **Ladder order and shape.** One rung is tried on *every* failed connection before the next rung starts, and rescue
   runs **before** min-cut and permutation (`RESCUE_FIRST = True`; `tests/test_v2_repair.py --design-order` and
   `repair_trace.py --set RESCUE_FIRST=0` run the design's order). Measured on `synth_qfp_fanout` when it had 21
   failures after detail: one connection at a time in the design's order repaired 4, one connection at a time with
   rescue first 10 — the connections late in the rank order never got budget. Rung by rung, the two orders were
   within one connection of each other on `synth_qfp_fanout`, `interf_u` and `stickhub`, and rescue first was cheaper
   (140 k against 423 k units on `synth_qfp_fanout`). Within rescue the cheapest rung is first: extra via, half pitch,
   neck, re-escape.
9. **Acceptance of a rip**: score improved **and** every victim (and tee dependent) routed again. "Score improved"
   alone would accept swapping one open connection for a shorter one.
10. **After a rip that did not pay** the blamed victims are hard for the rest of that connection's min-cut rung (the
    next soft search has to find another cut; at most 3 soft searches), in addition to the 0.3 mm of history.
11. **Victim re-route**: S4 steps 1–6, then once more with `widen=2`, `extra_vias=1` and the layer assignment lifted.
12. **Cluster** = the failed connection + other failed connections in the box + its min-cut victims + what those
    victims fight (one extra soft search when a victim cannot be re-routed) + connections ripped twice. The design's
    "failed or ripped twice" alone never contains the routed connection that has to move. The orders are walked as a
    tree (a shared prefix is routed once, a prefix that cannot beat the best is skipped), and the permutation is not
    started when a failed member cannot be routed even with the whole cluster ripped. A cluster is permuted once.
13. **Failures no search can repair** (`code` `outside` / `layer_pad`) skip every rung and the feedback loop.
14. **Certificate cut**: for a boxed-in terminal the widest gap between two neighbouring pieces of fixed copper
    (`Tiles.pad_cut` when both are pads and tiles exist, else `oracle.pad_gap`); else the tightest transversal through
    the contested spots — fixed copper on both sides, `capacity = gap − cl`, `demand = Σ (w + cl)` of the failed
    connection and every routed track between the walls — reported only when it is over capacity; else
    `Tiles.cut_for_edge` of the failure's tile border; else None (diag then derives its own).

**Findings for other packages** (measured with `bench/route/repair_trace.py`, KiCad 10; the sibling stages changed
between runs, so the counts are snapshots):

15. **W4 / W5 — `layer_pad` plane connections**: 24 of 40 detail failures on `stickhub` and all 11 on
    `multichannel_mixer` were pad→pour connections failing with "pad terminal is not on any layer this net may use"
    (terminal b has no layers). Repair leaves them alone; legacy / the pours pass close them.
16. **W3 / W4 — single-layer corridors**: in the first runs every failed connection on `synth_qfp_fanout` (21) and 44
    of 50 on `interf_u` had `allowed_layers == (0,)` on a 2-layer board; lifting the layer assignment (point 6)
    repaired 9 of the 21 with one search each. Later sibling versions failed far fewer connections this way.
17. **Rips rarely pay on the congested boards**: `interf_u` 2 of 21 accepted, `stickhub` 4 of 11; the usual reason is
    "victim cannot be re-routed: exhausted". Permutation solved no cluster on the three boards traced (it does solve
    the synthetic 3-net order puzzle of the unit test).
18. **W3 — feedback cost**: one feedback loop on `interf_u` spent about 4 s of wall time inside `globalroute.run`
    (212 tile A* calls for 12 connections) and gained 0 or 1 connection.

## W2 — addendum (pours pass, after the first corpus runs)

14. `judge.pours_pass` order: the plain rounds first (`POUR_ROUNDS = 2`: dog-bones when `dogbones`, then the search),
    then at most `GIVE_ROOM = 2` attempts to give crowded pour pads room (regions only, no hardened spokes: the pour
    does not reach those pads yet). A give-room attempt is started only when `judge._room_cost` (2 × the crowd's share
    of the search pass, from `ctx.history['search'] = {work, conns}` set by the engine) fits in the pours budget, and
    is kept only if the *next* pours round leaves the board better than before the attempt.
15. Pour stubs KiCad rejects: blamed by uuid / neighbourhood when a marker names them, else the new stub nearest to
    the marker (≤ 5 mm), ≤ 3 removal rounds; only then is the whole round undone. Dog-bones that do not reduce
    KiCad's open count are undone and the search gets the next round.
16. Starved thermal: all connections blamed for one pad in one round are re-routed together (the pad is closed
    once); the "already given room → remove" rule applies from the next round on.
17. Environment switches read by the engine (tests / benchmark only; `bench/route/routers/v2.py` sets them from its
    options): `COPPER_V2_DETAIL=legacy|window`, `COPPER_V2_CLASSIFY=full|minimal`, `COPPER_V2_STRICT=1`.

**W1 measurements** (KiCad 10, this machine while other benchmark jobs ran; rerun lines given)

20. Legacy stage alone, no judge (`bench/route/legacy_probe.py --twice` → `results/legacy_probe.json`): signals pass,
    commit, ≤ 2 pours passes, one DRC. **1889 / 2186 = 86.4 %** (v1 baseline 81.8 %), 12 new DRC errors left
    unrepaired (10 `starved_thermal`, 2 `hole_clearance` of point 17), 6 / 14 boards complete and clean, 14 / 14 give
    identical copper twice. 0 pieces illegal for the oracle, 0 connections lost to the self-check (v1: 75 on
    `one_air_max`), 0 connections "routed" that are not `connected(loose=True)`, 0 nets the router calls routed that
    KiCad lists as unconnected. `one_air_max` 97.5 % (v1 86.4), `interf_u` 85.4 (67.7), `coldfire` 93.1 (90.2),
    `stickhub` 95.3 (92.2), `tinytapeout` 43.2 (38.1), `cm5_minima` 90.9 (89.9); `multichannel_mixer` 98.3 (98.8:
    one connection fewer than v1 *with* its repair loop). The emit-time check fired 4 times on the corpus
    (`coldfire` 3, `one_air_max` pours 1), each fixed by one re-raster; the final self-check never fired.
    Search throughput 100 - 290 k expansions/s (v1: 170 - 290 k): the RouteState / oracle bookkeeping is not
    measurable against the search.
21. `RIP_LEVEL = "net"` (v1's negotiation) on the same probe (`--rip net` → `results/legacy_probe_ripnet.json`):
    1869 / 2186 = 85.5 %. Connection-level rip is better or equal on every board (`coldfire` 93.1 vs 91.6,
    `interf_u` 85.4 vs 82.3, `tinytapeout` 43.2 vs 41.5, `cm5_minima` 90.9 vs 89.9; but it leaves 12 new DRC errors where net-level leaves 9) and
    stays the default.
22. **W2 — build stage 1 through the harness** (`run.py --router v2 --name v2_stage1_w1 --opt stage1=true --runs 1`,
    tree as of this note): 1831 / 2186 = **83.8 %**, 0 new DRC errors, 6 / 14 clean, 622 s router wall time. Above the
    baseline in total, but **two boards are below their baseline**, which the stage-1 gate forbids: `interf_u` 65.2 %
    (baseline 67.7; the legacy stage alone leaves 85.4) — the judge removed 24 connections for `starved_thermal`
    markers and took the pours round out again; `cm5_minima` 88.5 % (baseline 89.9; legacy alone 90.9). Also
    `one_air_max` 91.5 % against 97.5 % for legacy + two plain pours passes (pours round 2 "did not leave the board
    better" and was taken out). The loss is after `legacy.route_all` returns (same 136 / 159 and 213 / 214 signal
    connections as the probe); it is in the judge's removal policy, not in the search.

## W4 (window, search, detail) — as built

Files: `v2/window.py`, `v2/search.py`, `v2/detail.py`; tests `tests/test_v2_window.py`, `test_v2_search.py`,
`test_v2_detail.py` (+ `tests/v2_w4_support.py`); measurement `bench/route/w4_detail_bench.py`. Nothing in §9 is
renamed; everything below is additive or fills a point §9 leaves open. Measured numbers are at the end.

**window.py**

1. `build(ctx, conn, tiles_mask, box, *, width, pitch=None, soft=False, layers=None, cache=True)`: `layers` = board
   layers of the window (default `window.allowed_layers(ctx, conn, corridor=True)` = plan `layers_ok` narrowed by
   `Corridor.layers` unless that cuts a terminal off; `corridor=False` = the plan's layers only); `cache=False`
   bypasses the LRU. `soft=True` only adds the bookkeeping of cells
   covered by more than one routed piece (`win.multi` / `win.vmulti`); dynamic copper is in `owner` either way.
2. `Window` as built: `grid, pitch, layers, lidx {board layer: index}, box, cm, owner, vowner (per 2-D cell: piece
   whose halo covers a via centre), multi, vmulti, cmul (array 'f' | None), hist (array 'f' | None), net, net_id,
   width, clearance, via, col_tile / row_tile / tile_t (when built with a mask), ends, last_reject`; methods
   `cell(x, y, layer)`, `xy(state) -> (x, y, board layer)`, `free(state)`, `block(states)`, `block_via(cells)`,
   `reset()`. **State = `lidx[layer] * G + iy * W + ix`**: the layer part is the index in `win.layers`, not the board
   layer (a 2-layer window on a 6-layer board has 2 blocks).
3. `cm` is a `guided.grid.ClassMap` with only `S / SV` (static) and `occ / vocc` (static + routed copper) filled;
   its reference-count arrays are not allocated (`mark` / `unmark` must not be called). Values: 0 free, -1 blocked,
   **`window.MASKED` (-2)** = outside the corridor mask or on the window border. The search treats -2 as blocked but
   reports having touched it (`info['open']`, `probe()['open']`): "a wider corridor could go on here". The window is
   built per net (same-net copper is simply free), so `net_id` in `search` only matters for hand-made maps.
4. Inflation is `need + half_width - TOL_MM + max(GUARD_MM, raster_guard(pitch, half_width + need))` per shape (W0
   note 13). Measured consequence: **0 emit-time rejections** on all 14 corpus boards (1 600 routed connections), and
   `tests/test_v2_window.py::test_free_cells_and_steps_between_them_are_oracle_legal` checks every step between two
   free neighbours with the oracle. Via centres use `need(kind='via') + radius - TOL_MM + GUARD_MM`, on **every board
   layer** (not only the window's), plus v1's "no via in an SMD pad of the own net" (0.6 x radius).
5. Pitch rule (c): when `cells x layers > 400 000` the pitch is coarsened (ladder extended by 0.3 / 0.4 / 0.5 mm);
   the window is **not** split at via hints (`Corridor.layer_seq` is not used by W4 at all, nor is `lateral`).
   Origin: mode of the pad residues as designed, **but a fine-pitch terminal of the connection (or an escape-stub
   end) overrides it** — a pad between at-the-rule neighbours can only be left along its axis, which needs its own
   centre on the lattice.
6. Corridor mask needs the tile geometry: `window.tile_origin(tiles)` reads `tiles.x0 / y0 / t` (present in W3's
   `Tiles`), else derives the origin from `tile_of` by bisection. Cells within 1 mm of either terminal are never
   masked off.
7. Prices stamped into `win.cmul`: soft shapes of other nets x6 (`SOFT_PRICE`), one cell round a foreign pad's halo
   x1.25 (`PAD_REPEL`), plan regions (`cfg.regions`: cost / forbid, so the judge's `"judge"` regions are honoured),
   `NetCfg.avoid`, `NetCfg.corridor`. `win.hist` comes from **`ctx.history['points'] = [(x, y, radius_mm, cost_mm[,
   layer]), ...]`** (the format W7 writes). `ctx.history['marks']` (W2) is not read: its regions already price it.
8. LRU: 8 windows per oracle, keyed by box, pitch, origin, layers, net, width, clearance, via, mask, `soft`, the
   terminals, the ids of the routed pieces / soft / hardened shapes near the box, the history points and the plan
   regions of the net — not by the global `oracle.version`, so a rollback to an earlier state hits again. A hit
   returns the same object after `reset()` (blocks undone). Work is charged per build call, hit or not.

**search.py**

9. Costs are in **grid steps**: callers pass `via_cost` and soft prices divided by `win.pitch` and
   `turn = astar.turn_table(bend_cost_mm / pitch)`. `via_cost=None` = no layer changes. `layers_ok` = board layers.
10. `search(..., moves=None)`: optional per-window-layer move tables (`astar.layer_moves`) for layer cost / direction
    bias; default uniform. `soft_cost`: a number, or `{piece id: steps per halo cell}` with key `None` as the default
    (a piece without a price stays hard). `info` also has `best` (closed state nearest the target) and `open`;
    `frontier` is a list of states. `search.crossed(win, net_id, path)` recomputes the crossed pieces of a path.
11. `heuristic(win, targets, tile_dist=None, hweight=1.0)` returns a `list` subclass that is filled lazily (entries
    < 0 are not computed yet; `h.fill(cell)`), indexed by 2-D cell (`state % G`), already times `hweight`. A plain
    list of floats >= 0 of length `G` works in its place; `h=None` is Dijkstra.
12. `probe(win, net_id, sources, limit, budget, *, layers_ok=None, vias=True)`; the result also has `open` and
    `frontier` (`[(free state, blocked neighbour state)]`, <= 16); `frontier_blockers` keys are piece ids and the
    string `'static'`. Charges one unit per expansion (no `CHARGE_SEARCH`).
13. Plain search keeps v1's "incoming direction of the best arrival" (measured: within 25 % of the exact optimum at
    a bend cost of 1.5 steps, exact at bend 0); `bend_state=True` is exact (tested against brute force) and ~2.4x
    slower. Detail does not use it by default (`detail.BEND_STATE_CELLS = 0`).

**detail.py**

14. Step 2 (probe) differs from §4.8: no separate small windows. The target side is flooded (2 000 expansions) **in
    the step-3 window**; the start side needs no flood — a start that is walled in makes the A* run dry within
    2 000 expansions, which is reported the same way (`probe.enclosed = 'a'`). Trees with more than 64 target cells
    are not probed. A search that ran dry without touching a `MASKED` cell marks that width as dead: wider rungs
    are skipped (they cannot help), the neck width is still tried.
14b. **Layers per rung**: the un-widened corridor rung searches only the layers the global stage planned
    (`Corridor.layers`); the `corridor+2` and `wide` rungs (and any call with `widen > 0`) use every layer the plan
    allows. Measured with W3's corridors on `interf_u` / `one_air_max`: 77.4 % / 89.3 % when the planned layers bound
    every rung, 85.5 % / 97.7 % with this rule.
15. `route_conn` result: `{ok, pieces, fail, crossed, how: 'pattern'|'window'|'already', width, step}`. On success
    the pieces are committed in one transaction, `state.connected(conn)` is checked **before** commit (else
    rollback) and the status is set to `'routed'`. On failure the state is untouched and **the status is not set**
    (`detail.run` sets `'failed'` with the fail record; repair decides for itself).
16. `fail` = `{reason (text, v1 phrases), code, where, blocked_by, blockers, static_share, tried, probe:
    {'enclosed': 'a'|'b'|'none'}, frontier: [[x, y]], width}`. `blocked_by` = net names of **routed** copper in the
    way, ranked (v1 format; what diag / repair / engine read); `blockers` = `[{net, tag, static, conn, count}]`
    including static pads. `code` is one of `enclosed, no_start, no_target, exhausted, limit, budget, max_vias,
    rejected, not_connected, outside, layer_pad, plan`. `static_share` after an `exhausted` / `enclosed` failure comes
    from a full re-flood of the failed window; after `limit` it is local (2 000 cells round the start).
17. `soft_cost` (repair): a rip price in **mm per crossed connection** — a number, `{conn idx: mm, None: default}`,
    or `callable(Piece) -> mm | None` (None = that piece stays hard). Detail spreads it over the cells of the
    piece's halo. When the cheapest path crosses something nothing is added and the result is `{ok: False, plan:
    True, crossed: {conn idx: pieces crossed}, crossed_pieces: {piece id: cells}, fail: {code: 'plan', ...}}`; when
    it crosses nothing it is emitted like a normal route.
18. Patterns: besides terminal b, a `tree_target` connection first tries the same shapes to <= 3 tee points per
    layer on its net's routed tree that are at least 0.5 mm nearer than b. A pattern must stay inside the corridor's
    tiles, may not cross a soft shape of another net, and is not tried at all when the net has `avoid` / `corridor`
    or a plan region (judge regions included) overlaps the terminals' box — the window search prices those.
19. Targets of a `tree_target` connection: terminal b plus the cells along same-net pieces on b's side. Sides come
    from a union-find over the net's *routed* connections (keyed by `Terminal.cluster`, else pad name); loose pieces
    (`conn == -1`, this connection's own) are on a side when they touch its terminal point. Kept tracks / vias of
    b's static cluster are tee targets too. If the result does not satisfy `state.connected`, the rung is retried
    once with terminal b as the only target.
20. `emit(ctx, conn, win, path, width)` reads the end anchors from `win.ends = ({state: anchor}, {state: anchor})`
    (`('pt', x, y)` or `('seg', x1, y1, x2, y2)`), set by `_route_window`; without it no joins are made. On a
    rejection it returns the `Blocker` and sets `win.last_reject`.
21. `detail.run` report: `data` has `how, fail_codes, failed (list of {conn, net, from, to, reason, code, where,
    blocked_by, static_share, tried, probe}), windows, searches, expansions, median_expansions, emit_rejects, joins,
    necked, window_builds, window_cache_hits, build_s, search_s, emit_s, pattern_s, build_share` (the `_s` values
    are wall time for the report only; no decision reads a clock). Running counters live in
    `ctx.history['detail']`.
22. Without `ctx.corridors[conn.idx]` (or without `ctx.tiles`) the corridor is a box: both terminals grown by
    10 % + 1.5 mm, + 1 mm per ring. The wide rung is always the box of both terminals + 25 % + 5 mm.

23. **Answers to notes addressed to W4.** W8 (via removal): `detail.route_span(ctx, conn, a, b, layer, budget, *,
    width=None, margin=1.5) -> [(x, y), ...] | None` now exists — a single-layer window search between two points
    for the connection's net, every segment oracle-legal, nothing added to the state (tested; polish does not call
    it yet). W7 note 16 (single-layer corridors): point 14b. W7 note 15: a terminal without layers (a pour
    connection) still fails with code `layer_pad`, now worded "no copper terminal (a pour connection): left to the
    pours pass". W7 note 1 (a failing `route_conn` costs up to 6 windows): unchanged — a width whose search ran dry
    without touching the mask skips its wider rungs, nothing else is saved.

**Not done / left to others**: window split at via hints and `Corridor.layer_seq` / `lateral` hints (W3's hints are
ignored; the layer choice is the search's); half-pitch rescue, extra via and soft search exist as `route_conn`
arguments but the ladder that uses them is repair's (W7); pad-entry prettiness (W8): a pad that is off the lattice is
joined by sliding the first run through it (no stub) when that is legal, else by a short 45 degree jog.

**Measured** (KiCad's Python 3.11 on this machine, which other work packages were loading at the same time: wall
times moved by up to 1.7x between runs, work units did not). `bench/route/w4_detail_bench.py` runs the detail stage
**alone**: connections from v1's `plan_connections` (signals pass, 1 804 connections on 14 boards — not the
benchmark's 2 186, which include the pour connections), no power / fan-out / repair / polish / judge. This is not a
benchmark score; it measures W4's files.

| | box corridors | W3 corridors (`--global`) |
|---|---|---|
| connections closed by pattern + window search alone | 1 596 / 1 804 (88.5 %) | 1 596 / 1 804 (88.5 %) |
| pieces the oracle rejects after the run / emit-time rejections | 0 / 0 | 0 / 0 |
| same twice (every count and the work units) | 14 / 14 boards | not repeated |
| window builds / searches | 1 355 / 1 199 | 1 878 / 1 713 |
| expansions, median per search (per board) | 18.7 M, 552 - 22 425 | 13.4 M, 173 - 5 584 |
| search throughput | 177 - 350 k expansions/s | same code |
| window build | 1 - 32 ms (13 ms mean on the two densest boards: ~25 k cells x 2 layers, ~300 shapes) | same code |
| build share of the stage's time (sum over the corpus) | 20 % | 30 % |
| stage wall time, sum of 14 boards / worst board | 130 s / 55 s (`coldfire`; 32 s on a quiet run) | 103 s / 32 s |

- **Cost gate of §4.7 (build <= 30 % of detail work)**: met with box corridors (20 %), exactly at the limit with real
  corridors (30 %; more, smaller windows). Per board it is above 30 % on 4 boards with box corridors (`stickhub`
  57 %, `tinytapeout` 40 %, `sonde_xilinx` and `multichannel_mixer` 33 %) and on 8 with W3's corridors (31 - 70 %;
  six of them finish the stage in under 3 s). Half of a build is the via map
  (6.5 of 13 ms); `oracle.near` is 1 ms; geometry (span generation) is a quarter. Tried and removed: sharing span
  lists between windows on a common lattice (the §4.7 "per-tile raster cache" idea at shape level) — identical copper,
  no gain (36 - 51 distinct lattices per board, and translating cached spans costs what it saves). What would help: a
  net-independent static map per tile (v1-style owner ids) that windows copy rows from, or building the wide window
  once and deriving the corridor rungs from it (60 % of the builds belong to connections that fail).
- §8 budget "window build <= 15 ms for 50 k cells x 2 layers": 5 ms on a synthetic window with 125 shapes
  (`test_build_speed_report`); on real dense windows the shape count dominates and the budget is missed by about 2x.
- §8 "window search median <= 8 k expansions": met on 12 of 14 boards with box corridors (`coldfire` 11.9 k,
  `synth_bus_cross` 22 k) and on all 14 with W3's corridors.
- `kicad-cli pcb drc` after committing the result to a scratch copy (box corridors, all 14 boards): the error count
  is unchanged on 11 boards and the only new errors are `starved_thermal` (`interf_u` 4, `complex_hierarchy` 1,
  `multichannel_mixer` 1) — a zone-fill outcome S1's spokes are meant to prevent; no new clearance / short / hole /
  edge / keep-out / mask error. (Compared as counts by type against the unrouted board, not marker by marker.)
- Odd-angle segments 0 - 1.6 % per board; segments shorter than 0.2 mm 0 - 20 % (`stickhub` 20 %, median board 6 %).
  On the two boards looked at (`stickhub`, `one_air_max`) four in five of them touch a pad: the 45 degree jog a
  pattern or a join needs when two pads are a few hundredths of a millimetre out of line. Not fixable at 45 degrees
  without moving a pad; W8's pad-entry pass is the place.
- What fails in this isolated run (208 connections): `limit` 46 and `exhausted` 60 (mostly routed copper in the way:
  repair's job), `enclosed` / `no_start` / `no_target` 102 (pads fenced in by earlier routes, 67 of them on
  `tinytapeout`'s 0.4 mm QFN: fan-out's job).

## W3 (tiles, globalroute) — S3, as built

Files: `v2/tiles.py`, `v2/globalroute.py`, `tests/test_v2_tiles.py` (15 tests), `tests/test_v2_globalroute.py` (19),
`bench/route/tiles_cal.py` (`cal` / `global` / `extract`; results in `bench/route/results/tiles_cal/`). Design §4.6,
§9 (the two interface blocks) and Appendix A3 are updated in place. No v1 file and no other v2 file was touched.

**Interface as built** (everything in §9 exists with the §9 signature; the rest is additive)

1. `Tiles(board, oracle, tile_mm, ref_w, ref_cl, cal=1.0, *, via=None)`; `nx, ny, nl, t`; also `G, N, x0, y0, pitch,
   full, cal, advisory, via_size, via_drill, version`. `tiles.make(board, oracle, conns=None, tile_mm=None, cal=None)`
   is what the engine / S3 use (tile size by `choose_tile_mm`, reference class = most common `(width, clearance)` of
   the connections, `CAL`). Sides: `'E'` = border to `(tx+1, ty)`, `'N'` = border to `(tx, ty+1)` (+y of the board
   file); `'W'` / `'S'` are accepted. Flat arrays for hot loops: `capE / capN / hardE / hardN` indexed
   `(layer*ny + ty)*nx + tx`; a border "code" is `index*2 + (0 E | 1 N)` (`edge_of(code)`).
2. Additive methods: `raw_cap`, `tracks`, `slack`, `passable`, `free_intervals`, `blocked`, `pad_edges(pad)`,
   `own_shadow(code, pos, net_id, half)`, `center`, `rect`, `scale_rect(rect, factor, layers)`,
   `book_shape(layer, shape, grow)`, `neighbour_cuts(ref, layer, w, cl, max_gap=3)`, `set_usage` / `utilisation(layer)`
   (the congestion picture of the kept iteration, percent per tile), flags `reservations_booked` (read-only alias
   `_reservations_booked` for diag) and `regions_scaled`. Module: `CAL`, `CAL_GATE`, `CORRIDORS_ADVISORY`,
   `choose_tile_mm`, `min_pad_pitch`, `reference_class`, `default_via`, `reference_demand(tiles, tracks, own=True,
   by='count'|'width')`, `calibration(tiles, tracks, cals, by)`, `box_span / poly_spans / shape_spans`.
3. `globalroute.run(ctx, budget, only=None)`, `pathfinder(tiles, conns, cfg, x, budget, *, dirs=None, state=None)`,
   `assign_layers(tiles, conn, path2d, cfg, x, *, state=None, end_layers=None) -> (layers, layer_seq)` (`((), ())`
   when no assignment exists), `layer_directions(board, conns, tiles=None, layers=None)`; class `GlobalState` (usage,
   history, 3-D tile paths; `negotiate`, `route`, `rip`, `overflow`, `crossings`, `assign`, `relayer`).
4. What S3 publishes: `ctx.corridors[idx]`; `ctx.tiles` (built here when the engine has none);
   `ctx.history['global'] = {'path': {idx: [(tx, ty)]}, 'layer': {idx: [layer on which the path leaves each tile]},
   'tee': {idx: host idx}, 'crossings': {(layer, tx, ty, side): n}, 'cuts': [Cut], 'overflow_mm', 'tile_mm',
   'layer_dirs', 'advisory'}`; `ctx.history['global_state']`; `x.auto['layer_dirs']` (layer names);
   `StageReport.data`: `iterations, overflow_mm, overflow_first_mm, wirelength_mm, expansions, searches, relayered,
   crossings_left, open_skipped, tile_mm, nx, ny, layers, cuts (dicts), vias, layer_dirs, booked, regions_scaled,
   advisory, out_of_budget`.

**Deviations from the design, each measured** (details: Appendix A3)

5. **Capacity = the design's formula + slack.** `raw_cap = Σ floor((len_i + cl) / p) × p` is kept (`raw_cap`,
   `tracks`), but `cap()` — what demand is compared with — adds the largest remainder `(len_i + cl) mod p` (0 when no
   reference track fits). Flooring to the reference pitch made a single power track overflow every empty border it
   crossed: 129–239 false cuts and 30 non-converging iterations on `complex_hierarchy`, `pic_programmer`, `interf_u`,
   `phone_breathalyzer`. Slack never admits one more reference track.
6. **A free interval cut by a tile corner** is counted with its continuation in the next border (the larger piece
   keeps its floor, the smaller gets `floor(joint) − floor(larger)`), otherwise a gap that straddles a corner is closed
   (or double counted).
7. **Own-pad shadow.** Borders touched by the inflated copper of a connection's own terminal pads are free for it
   (`pad_edges`): a pad row's shadow has no capacity for anyone else, and without the exemption every escape is
   overflow.
8. **Via capacity**: a tile-anchored sample lattice (spacing ≤ half the via pitch); capacity = legal share ×
   T² / pitch², rounded down with a quarter of slack, at least 1 when any sample is legal. The design's global
   `via + cl` lattice leaves most 0.5 mm tiles without a lattice point (0.8 mm pitch: 61 %) by aliasing alone. The
   variant over-counts when T is below the via pitch.
9. **Crossings** (not in the design; its only tool was layer directions for ≥ 40 connections). Border capacities
   cannot see that two nets cross: `synth_bus_cross` "converged" with 0 vias. Now (a) an exact test per iteration —
   two paths of different nets sharing a run of tiles on a layer must cross iff the second leaves the first on
   different sides at the two ends of the run — whose hits are overflow, re-route both and add node history, and (b)
   a cheap price in the search (straight-through traffic of the other axis; a turn in a tile with through traffic
   costs one via). Planned vias after this (v1 planner's connections, pour stubs excluded): `synth_bus_cross` 46 (v1
   routed it with 50), `interf_u` 101 (human 84), `multichannel_mixer` 26 (human 29), `coldfire` 398 (human 253),
   `tinytapeout` 513 (human 405). The test is exact for tile paths only; a run that holds a terminal proves nothing
   and is skipped.
10. **Search**: A* aims at the target tile nearest to the source when the target is a tree of many tiles (the
    bounding-box distance is 0 inside the box and the search flooded: 2/3 of all expansions on `tinytapeout`); the
    heuristic uses `cfg.hweight` (1.15, v1's value); searches stay in the box of their terminals + 8 tiles (+2 per
    iteration) and fall back to the whole board. So a path is not always the cheapest one to the *tree*.
11. **Layer DP**: also refuses a re-layering that adds overflow or a crossing (it prices them, it must not trade a
    via for them); a pad on a layer its net may not run on is reached with a via in the pad's own tile.
12. **`reference_demand`** defaults to the design's wording (count tracks: one reference pitch per crossing);
    `by='width'` is the stricter reading. See A3 for both numbers.

**For other packages**

13. **W4 — `Corridor.layer_seq`** entries are `(i, from_layer, to_layer)`: a via in tile `path[i]`, where `path` is
    `ctx.history['global']['path'][idx]` (also set as the extra attribute `corridor.path`, which
    `dataclasses.replace` drops). `types.Corridor` has no ordered path; a `path` field would be the clean fix.
14. **W4 — tees.** A `tree_target` connection may end on the tile path of a same-net connection instead of its
    terminal b (`history['global']['tee'][idx] = host idx`); its corridor then ends at that tile and need not contain
    b. Only hosts that precede it in `rank` order are used (`TEE_ONLY_EARLIER`), so in rank-order detail routing the
    host's copper exists. Detail re-sorts by overflow within a plan rank: where that puts a tee before its host the
    corridor leads to nothing yet. Without the rule about half of all tees had a later host (51 of 107 on
    `tinytapeout`); with it the global stage needs about 30 % more work there.
15. **W4 — `Corridor.layers` is a plan.** It is crossing-aware now, but it is still a tile-level model: keep the
    escape hatch (repair's `replace(cor, layers=())`).
16. **W4 / W7 — pad-to-pour connections** (open terminal b) get no corridor (`data['open_skipped']`).
17. **W5 — bookings.** `run` books, once per `Tiles`: protected / `power` / `fanout` pieces of `ctx.state` (width +
    clearance per border crossed, one via per column) and the oracle's soft shapes (`book_shape`; a multi-layer
    `reserve` disc = one via). If `history['power']['book']['booked']` is set, power's own entries are left to
    `power.book`; in every case the flag is set afterwards so nothing is booked twice.
18. **W7 — feedback.** `run(..., only=[...])` re-routes only those connections, judges the negotiation on their own
    overflow, stops after 4 iterations without improvement and at 20 at most (the 4 s you measured was 30 iterations
    chasing overflow the subset could not touch). Measured: 12 connections on `interf_u` 0.7 s, on `tinytapeout` 1.0 s.
19. **W9 — plan hints read** (all optional, objects or dicts): `x.tile_mm`, `x.negotiation_iters`, `x.nets[net]`
    `{via_budget, waypoints, between, side_of}`, `x.buses[{nets, order, layer}]`, `x.regions[{rect, layers, nets,
    exc, capacity_scale, no_vias}]`, `x.auto`. A `capacity_scale` region that names nets is ignored (a border has one
    capacity); `no_vias` is honoured per net. `side_of` is a barrier ray: tiles on the wrong side of the part within
    its extent along the connection's direction cost × 5. Bus members pay half on a border a sibling uses; `order`
    only fixes the lateral rank where geometry does not.
20. **W2 — budget.** On `tinytapeout` the stage does not converge inside 10 % of the default budget (1.8 M units):
    it stops after 14–17 iterations with 2–4 mm of overflow (about 20 cuts) and says so in `notes`. Every other
    corpus board needs between 0.3 k and 1.5 M units.

## W5 (classify.py, power.py) — as built

Files: `v2/classify.py`, `v2/power.py`, `tests/test_v2_classify.py` (21 tests), `tests/test_v2_power.py` (21 tests).
Nothing outside those files was edited; the design doc is unchanged, the differences are listed here.

**`classify.py`** — §9 signatures as written (`run`, `roles`, `detect_buses`, `connections`), plus:

```python
net_table(board, cfg, x=None) -> {net: {role, why, kind, analog, width, neck, clearance, via, skip, unskipped}}
widths(board, cfg, x, roles) -> {net: (width, neck)};  buses(board, x=None) -> declared + auto buses
decoupling(board, roles) -> [(power net, capacitor pad, IC pad)]
net_kind(board, net) -> 'ground'|'power'|'';  is_analog(board, net) -> bool;  pad_minor(pad) -> mm
```

1. `run` fills **`ctx.conns` in place** (`ctx.conns[:] = ...`, the `RouteState` built before S0 holds that list),
   merges into `ctx.x.auto` (`roles` (non-signal, not from the plan), `buses`, `widths`, + `analog`, `unskipped`,
   `decoupling`; `layer_dirs` untouched) and writes `ctx.history['classify'] = {nets (the table above), buses,
   decaps [conn idx], skipped [v1 format], auto}`. `StageReport.data` = `{roles, auto, skipped, decaps,
   connections, by_role}`. It calls `oracle.set_clearance` for nets whose plan clearance is above the class.
2. **`x` is read defensively** (`planx.V2Cfg`, a plain dict or None all work): `x.nets[name].role / .tree`,
   `x.power_width_factor`, `x.buses` (uses `order`, else `nets`), and in power `x.reserve` (`{'at', 'net'}`; the raw
   plan's `'for'` is read too).
3. **Poured nets get connections** (role `plane` unless the plan says otherwise; ranked last by v1's priority and
   `ROLE_ORDER`): one per connectivity cluster outside the main one, `a` = the cluster's pad nearest to other copper
   of the net, **`b` = an open terminal** (`pieces.is_open`: no pad, no cluster, no layers) located at that nearest
   pad — the same convention as `legacy.plan_connections(mode='pours')`. On the corpus S0 returns exactly the
   harness's connection count (2186 on 14 boards, = Σ (clusters − 1)).
4. **`neck` is `min(width, class width, Default-class width)`, not "the class width"** (§4.3). That is v1's
   `neck_widths` rule; with the design's wording a 0.4 mm power class can never leave a 0.5 mm-pitch pad
   (measured on `coldfire` U102 / U301: 13 pour pads had no dog-bone until the stub was allowed to neck to 0.2 mm
   beside its neighbour). `width` follows §4.3 (power: `min(class × factor, 0.8 × smallest pad minor, 1.0)`, floored
   to 0.01 mm, never below the class width). Pairs and `neck: false` have `neck == width`.
5. **Steiner order**: root = the largest cluster (pour-island area, pads, kept-track length; then lowest id), then
   repeatedly the cluster nearest to the tree = pads + kept tracks / vias + the octile doglegs of the connections
   already planned. `a` = the attaching pad, `b` = the **nearest pad** of the tree (a real anchor for engines that
   need one), `est` = octile distance to the nearest tree *point* (≤ the pad distance), `tree_target=True`.
   `tree: mst` = pad-to-pad Prim (`tree_target=True`), `star` / `chain` have `tree_target=False`.
6. **Decoupling pairs are decided in S0** (S1 only routes them): a 2-pad `C*` part with one pad on a `power` net and
   the other on a plane **or any ground-named net** (the design says "plane net"; a ground without a pour still
   makes it a decoupling capacitor), within 3 mm of a pad of the same power net on a footprint with ≥ 4 pads. Their
   connection (capacitor pad → IC pad, `tree_target=False`) is the first of its net; the rest of the net is
   decomposed around it.
7. **Buses**: two-pad nets between the same two footprints whose pads are row neighbours (mutual nearest
   neighbours, one constant step) on both parts; ≥ 4 nets; ground / power / poured nets and nets with a third pad
   never take part. Entry: `{nets, order (= nets, pad order on the first part), refs, pad_order: 'same'|'reversed',
   pads, pitch, auto: True}` (`order` is a list as in W9's convention; the same / reversed flag is `pad_order`).
   A physical bundle may contain a declared pair: its nets keep role `pair`.
8. `rank` = `(plan rank, −priority, ROLE_ORDER, est, net, idx)`; the N net of a pair takes its P net's rank, name
   and the matching connection's `est`, so it follows directly. `priority` is v1's (`cfg.nets[n].priority`).
9. **Finding (W1 bench adapter / W9 `default_plan`)** — `pcb_route_plan`'s suggested plan writes v1's automatic
   `skip: true` for a pourless ground into the plan, and the benchmark adapter sends that plan back as-is, so the
   skip arrives as an *explicit* plan skip and S0 honours it: on `coldfire` the 6 `GNDA` connections are still
   skipped under `run.py --router v2` (S0 routes them when given no plan: `auto.unskipped == ['GNDA']`). Fix on the
   plan side: do not emit that `skip` when `engine == 'v2'` (or run with
   `--opt plan='{"nets":{"GNDA":{"skip":false}}}'`).

**`power.py`** — §9 signatures as written, with one added keyword and helpers:

```python
run(ctx, budget) -> StageReport
dogbone_candidates(pad, via, clearance) -> [(stub_end_xy, via_xy)]     # stub_end_xy == via_xy
place_dogbone(ctx, pad, exclude=frozenset(), *, soft=False) -> [Piece] | None     # [] = softly reserved (soft=True only)
thermal_spokes(board, pad) -> [geom.Shape]                             # cls 'soft', one layer each, tag ('thermal', pad, k)
replace_dogbone(ctx, pad_key) -> [Piece] | None                        # the §4.4 rip rule in one transaction
neck_split(oracle, layer, x1, y1, x2, y2, width, neck, net, clearance=None, ignore=frozenset(), min_span=None)
    -> [(x1, y1, x2, y2, w)] | None                                    # local-only necking of one segment
pattern_shapes(ax, ay, bx, by) -> [polyline];  pattern_route(ctx, conn, budget=None) -> [(layer, x1, y1, x2, y2, w)] | None
book(ctx, tiles=None) -> int                                           # Tiles.book / book_via for spokes and dog-bone vias
pour_reaches(board, pad), needs_dogbone(board, pad), pour_penalty(board, net, x, y, r), lane_penalty(...),
lane_blocked(board, pad, x, y, radius, others), congestion(ctx, x, y), signal_layers(ctx), on_plane(ctx, net, x, y)
```

10. **Imports `classify`** (§9's table lists types, oracle, pieces, tiles): the per-net table, `_hint` and the row
    neighbours come from there. `tiles` is only duck-typed (`tile_of`, `via_cap`, `book`, `book_via`).
11. **Bookkeeping** in `ctx.history['power']`: `dogbones {pad key: {site, pieces [ids, via last], net, size, spokes
    [soft ids], shares?: owner key, tried?}}`, `soft {pad key: {site, ids, net, size}}`, `unreserved`, `why`,
    `spokes {soft id: (pad name, layer, k)}` (k 0..3 = the pad's spokes, 4..7 = its dog-bone via's), `reserve
    {site: soft id}`, `book`. The pad key is the pad name, or `name@x,y` when a footprint repeats a pad number.
    `StageReport.data`: `dogbones {key: [piece ids]}`, `power_unreserved [keys]`, `unreserved_why {key: 'no legal
    via site' | 'would wall in <pad>' | 'pour out of reach' | 'budget'}`, `soft_reserved {key: [x, y]}`, `shared`,
    `served_conns`, `closed_conns`, `decaps {routed, failed}`, `spokes`, `reserve`.
12. **Dog-bone pieces are `conn = -1`, `stage='power'`, `protected=True`**, the via last (as §3 and W7 expect). A
    plane connection whose pad got one is set to **`routed` when `state.connected(idx)` says so** (the via stands
    in a fill island of another cluster; `coldfire`: 156 of 165 plane connections close in S1, which took the
    detail stage's "not finished for lack of budget" from 193 to 38 there). KiCad's refill still has the last word:
    a pad the new fill misses comes back through the pours pass. Where the extract has no fills the connection
    stays `todo`.
13. **Deviations from §4.4, each measured on a board:**
    - *One dog-bone per unreached cluster*, not per pad (pads already joined need one).
    - *"Pad edge + via radius + clearance" is a true distance*: the via copper is `clearance` from its pad in every
      direction (along a diagonal the literal formula puts the via 0.085 mm from a 1.0 × 0.6 pad).
    - *A pad "too far from its pour"* (no candidate inside any outline of the net's pour: small local pours on
      `stickhub`, `one_air_max`, `cm5_minima`) is not a dog-bone case: reason `pour out of reach`, the plane
      connection stays for the search.
    - *Reservations must not wall signals in* (§13's risk, seen on `sonde_xilinx`: four 1.65 mm ground vias in a
      row at 2.77 mm pitch closed the three signal fingers between them, 100 % → 93.8 %). A candidate that leaves a
      neighbouring foreign pad's exit channel less than 0.2 mm of free track-centre positions (`lane_blocked`) is
      not used; such a pad gets a stub to a neighbouring dog-bone via of its net when one is within 5 mm of stub
      (`shares`), else it is `power_unreserved` with `would wall in <pad>`. Exit lanes are taken for every
      footprint (not only the pad's own), and a one-row part has lanes on both sides.
    - *Soft reservation where the pour is no plane* (`SOFT_ON_SHARED_LAYERS = True`): when the via would reach the
      pour only on layers the plan lets signals use (2-layer boards), S1 places no copper — a via there can be
      cut off from the pour by later tracks (seen on `sonde_xilinx`: the via ended in a pocket between two signal
      tracks) — and registers a soft `'reserve'` disc instead; `place_dogbone(ctx, pad)` without `soft` (the pours
      pass) puts the copper on that site if it is still legal. With hard copper instead (flag off) the four
      2-layer boards tried gave the same completion.
    - *A dog-bone via gets 4 soft `thermal` corridors of its own* on every layer where it stands in the pour.
    - *Candidate score* also adds 2.0 per soft shape of another net under the via (max 2) and 2.0 for a pour that
      is no plane; the pour-boundary penalty is evaluated lazily for legal candidates only (the argmin is exact).
14. **Local-only necking** (`neck_split`): legal at full width → one span; else bisect, a span that is illegal at
    full width and at most `max(0.4 mm, 2 × width)` long gets the widest legal width on a 0.01 mm ladder, never
    below `neck`; neighbours of equal width merge. Every returned span is oracle-legal; narrowed pieces carry
    `target_w` = the full width. S1 uses it for dog-bone and decoupling stubs; **W4 / W7** can call it for the
    "per segment, never per connection" rule of §4.4 / §4.9 instead of re-routing a whole connection at neck
    width (`coldfire` S1: 4.3 mm of 215 mm necked).
15. **Tiles**: S1 runs before the tile graph exists, so bookings wait in `history['power']['book']`.
    `globalroute` books every soft shape and protected piece itself and sets `book['booked']` (W3's note 17);
    `power.book(ctx, tiles)` does the same for a caller without S3 and is a no-op once the flag is set
    (`force=True` overrides). Side convention as in `tiles.py` ('E' = towards tx + 1, 'N' = towards ty + 1).
16. **`replace_dogbone`** refuses (None, nothing changed) when other pads share the via. A stage that removes
    dog-bone pieces itself and calls `place_dogbone(pad, exclude=sites)` (W7) is fine: stale records and corridors
    are dropped on the next `place_dogbone` call, a record put back after a rollback gets its corridors again, and
    a plan `reserve` site whose dog-bone was rolled back becomes a soft reservation again (W7's gap 4). A pad's own
    soft site is only given up when copper is really placed. Soft shapes are not transactional in the oracle, so
    between a rollback and that next call a few stale `thermal` corridors can still be priced.
17. **Spoke geometry is KiCad's default**, not the zone's: the extract carries no thermal gap / spoke width / pad
    connection type, so every pad in a pour gets 0.5 mm spokes over a 0.5 mm gap (+0.3 mm), along the pad's axes
    (diagonals for a round pad). A worker `extract` that adds `thermal_gap` / `thermal_width` per zone is read
    automatically.
18. **Finding (W1)** — `RouteState.add` costs ~1.5 ms per piece on `coldfire` (`_NetGraph.static_at` runs
    `geom.pt_in_poly` over 5 000-vertex fill islands): about a quarter of S1's time there (profiled). `power._Rings`
    (row-bucketed even-odd test) answers the same question in microseconds.
19. **Finding (W2, pours pass)** — on `synth_qfp_fanout` two ground pads stay open with and without S1 ("the new
    pad-to-pour copper did not leave the board better"), and on `stickhub` the pours pass connects fewer pour pads
    when S0 hands the plane connections to the detail stage first (9 pour-net opens vs 5 with the minimal classify;
    signals: 11 vs 17 open). Plane connections whose pad is `pour out of reach` may be better left to the pours
    search than to S4.

**Measured** (KiCad 10, this machine, other jobs running): classify 2 186 connections on 14 boards in 0.15–0.24 s
(≈ 10 000 connections/s, worst board 60 ms); power 1.6 s for all 14 boards (worst 0.45 s, `coldfire`: 157 dog-bones,
360 pad spokes, 909 oracle checks). Benchmark, `stages = classify, power, detail, judge`, today's tree, vs the same
without `power` (then S0 is the minimal one): `coldfire` 87.4 % vs 82.0 % (pour-net opens 2 vs 21),
`synth_qfp_fanout` 96.9 vs 93.8, `multichannel_mixer` 100 vs 98.8, `stickhub` 84.4 vs 82.8, `sonde_xilinx` 100 vs
100; 0 new DRC errors in every run, identical copper twice where two runs were made (`coldfire`, `stickhub`,
`sonde_xilinx`, `multichannel_mixer`, `synth_qfp_fanout`; results/w5_final, w5_power2, w5_nopower).
`coldfire` and `stickhub` are below the v1 baseline (90.2 / 92.2) in both variants, i.e. with and without S0 / S1;
where the rest of that gap comes from was not traced here (`coldfire`'s detail stage reports 38 connections not
finished for lack of budget).

## Integration — build stage 1 (classify-minimal → legacy → judge), as integrated

Integrator's changes on top of W1 / W2 (files: `v2/judge.py`, `v2/engine.py`, `bench/route/routers/v2.py`,
`tests/test_v2_judge.py`). No signature in §9 changed; everything is additive.

1. **Starved thermal, spoke-level blame first** (`judge._spoke_crowd`, `judge._protect_spokes`, switch
   `judge.SPOKE_BLAME`). For a marker "zone min spoke count N; actual K" on a pad that was not given room yet, the
   judge no longer removes every foreign connection in the pad's rectangle (v1's rule): it takes the pad's spoke
   corridors (`power.thermal_spokes`, when the module is built), drops the ones static copper closes, and frees only
   the `N − K` corridors closed by the fewest of this run's connections. Those corridors and the ones none of our
   copper touches are then closed to other nets: one forbid region per corridor (bounding box + 0.3 mm, that layer
   only, name "judge") and a hardened `thermal` soft shape in the oracle. The pad's key in
   `history['judge']['protected']` is `("spokes", layer)`; the next time the same pad is starved the rectangle rule
   applies as before (close rectangle, wide rectangle, then removal). Markers of the kind "n spokes connected to
   isolated island", pads without spoke geometry and the removal rounds after the repair rounds keep the rectangle
   rule. `_blame(ctx, chk, fine=False)`: the round loop passes `fine=True`.
   Measured on `interf_u`: 24 connections removed for starved thermals → 9 (with change 2: 4); 70.1 % → 77.4 %.
   The corridor test is a model: on `interf_u` it calls spokes "free" that KiCad does not count, so it is used to
   choose what to remove, never to predict the DRC result.
2. **Judge re-route budget when no repair stage runs** (`engine._Run.run`): the judge's share is
   `STAGE_SHARE['judge']` plus `STAGE_SHARE['repair']` when the repair stage does not run (build stage 1, or
   `repair` not selected / not built). It stays a floor, so only what the re-routes use is spent; a run whose search
   used its whole budget can end up to 27 % over it (v1's repair loop allows 50 %). Before, the 5 % was used up by
   the first repair round on `interf_u` and the next round's 9 connections were dropped without a re-route attempt.
   `interf_u` 77.4 % → 79.9 %.
3. **Reporting**: `StageReport.data['spoke_blame']` (judge) = `[{pad, lack, free, closed, freed}]`; the engine adds
   it to `out['judge']`, and the bench adapter now stores `out['judge']` (rounds, commits, spoke_blame) in the
   report it returns. Nothing in it is used for the score.

Stage-1 result of record: `bench/route/results/v2_stage1.md` (see design §12, "Stage 1 as measured").

## Integration — build stages 2 + 3 (roles / reservations / spokes / necking / polish; tiles / global / window detail), as integrated

Written by the second stage-2/3 integrator. The first one was cut off before writing notes; its code was audited
(all `tests/test_v2_*.py` green, full corpus re-run) and kept. What is described here is what the tree does, read
from the code and the result files, not a change log (there is no history of the intermediate states).

1. **Default pipeline** (`engine._Run.run`): classify (W5's, full) → power → fanout → global → detail (window
   search, corridors) → **legacy fallback** (`legacy.route_all(ctx, b, only=<open>, fair=True)`) → repair ladder →
   polish → judge → pours. The fallback and the ladder are not capped at a fixed share: each gets what the budget
   still holds minus the polish + judge shares. `engine.FALLBACK_FIRST` / `COPPER_V2_ORDER=fallback|repair` picks the
   order (default fallback first). `COPPER_V2_FALLBACK=0` gives the window search alone.
2. **Fair fallback** (`legacy.route_all(..., fair=True)`; `FAIR_FACTOR`, `FAIR_MIN`, `FAIR_FLOOR`): a connection's
   first attempt is capped at (work left / connections not yet tried), never below 12 000 units, by scaling v1's
   expansion limit. This is what removes "never tried": every open connection gets an attempt before any gets a
   second one. A capped attempt that fails is reported as "search effort limit reached", which is a tried
   connection.
3. **Second opinion** (`engine.route`, `ALT_MAX_CONNS = 250`, `ALT_MAX_WORK = 2.0`, `COPPER_V2_ALT=0` /
   `COPPER_V2_ALT_WORK`): when the staged pipeline leaves anything open (or a new DRC error) on a board of at most
   250 connections, and both runs together stay under 2 budgets, build stage 1's pipeline (minimal classify, v1's
   search for the whole board, then window detail + repair for what it leaves, polish, judge) routes the same
   unrouted board from scratch. The result with fewer new DRC errors, then fewer open connections, is returned;
   `out['portfolio']` lists both. The second run stops before the judge when it already has more open signal
   connections than the first result. An explicit `stages` / `detail` choice in the plan turns it off.
   This is a keep-best over two pipelines, not a fix of the staged pipeline: see point 6.
4. **Polish slack** (`POLISH_SLACK = 0.02`, `POLISH_SLACK_MIN = 1.0`): the engine lets polish add 2 % of the routed
   length (at least 1 mm) for clean-up edits and gates the stage on exactly that (`gate="tidy"`); W8's default was
   0, under which odd-angle entries stayed.
5. **Budget**: fallback, ladder and second opinion can take a run to 2 budgets of work in total. Wall time, serial
   run of record: see the table in design §12; `cm5_minima` is over the 180 s line of design §1.
6. **What the staged pipeline alone does** (`portfolio` rows of `bench/route/results/v2_stage3/*.json`, run 1):
   `phone_breathalyzer` 1 open (second opinion: 0), `synth_qfp_fanout` 3 (0), `stickhub` 12 (8), `cm5_minima` 22
   (20) — on these four the kept result is the second opinion, and without it `phone_breathalyzer`, `stickhub` and
   `cm5_minima` would be below their baseline. `interf_u`: staged 15, second opinion 30 (staged kept). Traced on
   `phone_breathalyzer` (`results/s3_noalt`, `--opt alt=false`): the open connection is the GND pour pad J1.B12
   (USB-C, 0.5 mm pitch). The pour reaches it on the unrouted board, so the power stage reserves nothing; routed
   signals and vias then close the pad off from its pour, and the pours pass finds no way ("giving pour pads room
   did not connect more"). Thermal spoke corridors are soft and do not stop this. Open for stage 4/5: a hard
   reservation (or a pour-connectivity check after detail) for pour pads whose only pour contact is one neck.
7. **Unchanged interfaces**: nothing in §9 was renamed. Additive: `legacy.route_all(fair=)`, `x.window_fallback`,
   `x.alt_beat`, `x.alt`, `out['portfolio']`, adapter options `fallback`, `alt`, `order`.

## Integration — build stages 4 + 5 (fan-out, repair ladder, global feedback), as integrated

Written by the stage-4/5 integrator. Fan-out (W6) and the ladder (W7) were already wired in by the stage-2/3
integration; what this build stage adds is what the benchmark showed to be missing around them. Files touched:
`v2/engine.py`, `v2/power.py`, `v2/planx.py`, `v2/judge.py` (one call), `bench/route/routers/v2.py`,
`tests/test_v2_power.py`, `tests/test_v2_planx.py`. Nothing in §9 was renamed; everything is additive. Result of
record: `bench/route/results/v2_stage5.md` (design §12, "Stages 4 + 5 as measured").

What was measured first (stage-3 tree, `bench/route/results/v2_stage3`): 148 connections open on six boards.
Three causes account for most of them, none of which the ladder as designed addresses:

* `coldfire`, `tinytapeout`, `cm5_minima`: the plan's `global.layers` (v1's default plan) leaves the plane layers
  out, so a 4- or 6-layer board is routed on its two outer layers. The human reference of `coldfire` carries a
  large share of its signals on In1 / In2.
* pour pads: about 40 of the 148 were pads of a poured net cut off from their pour by the routing.
* a net whose tree hangs on one unreachable pad loses every connection that ends there.

1. **Plane rescue** (`engine._Run.plane_rescue`, `PLANE_COST = 2.5`, `PLANE_SHARE = 0.15`; plan field
   `global.plane_rescue`, default true; COPPER_V2_PLANES=0). After the window search, the legacy fallback and the
   ladder, every connection that is still open, joins two copper terminals and belongs to a net without a pour
   gets one more `detail.route_conn(widen=2, extra_vias=2)` with the plane layers added to its `layers_ok` at
   cost x 2.5. Plane layers = `guided.plan.plane_layers(board)` (a pour covers >= 40 % of the board) that are
   inner layers. The connection's `NetCfg.layers_ok` / `layer_cost` are changed for the call and restored after
   it, so the power stage's notion of "plane" (`power.signal_layers`, cached in S1) and every other stage are
   unaffected. Whether the plane survives is KiCad's verdict: the judge sees a cut-off island as open
   connections / starved thermals. Measured: `coldfire` 41 open -> 22, `tinytapeout` 58 -> 46, 0 new DRC errors.
   Opening the plane layers for the whole run instead (`global.layers` = all four) was tried on `coldfire` and
   is worse: the power stage then reserves its 163 dog-bone sites softly and 20 pour pads end up walled in.
2. **Re-tree** (`engine._Run.retree`, `RETREE_SHARE = 0.15`; COPPER_V2_RETREE=0). S0 fixes which pad pairs a
   net's connections join. For each pad that ends two or more failed connections of a net, the shortest one
   keeps its terminals (it stands for the pad) and the others are re-aimed at each other's far ends:
   `ConnV2.a / .b / .est` are rewritten in place, the connection's corridor is dropped (box corridor), and
   `detail.route_conn` runs with the planes open. A re-aimed connection that still fails gets its terminals,
   corridor and fail record back. KiCad counts connectivity groups, so this is not a relabelling: on
   `tinytapeout` 9 connections close (41 open -> 32) while the 7 unreachable QFN pads stay open and reported.
   `RETREE_LEGACY` (v1's search for a re-aimed pair as well) is off: +1 on `coldfire`, but on `interf_u` the one
   extra track cut six ground pads off their pour (15 open -> 20).
3. **Plane ladder** (`engine._Run.plane_ladder`, `PLANE_LADDER_SHARE = 0.10`; COPPER_V2_PLANE_LADDER=0): last,
   `repair.run(only=<still open>)` with the plane layers open to every unpoured net, so the victim of a rip has
   somewhere to go. Yield is small: `coldfire` +1, `cm5_minima` +2..3, `tinytapeout` 0 (min-cut rips accepted:
   1 of 5, 0 of 14, 0 of 20). It costs 10 % of the budget; it is the first thing to switch off if wall time
   matters more than the last connection.
4. **Dog-bones for pour pads at risk** (`power.pour_at_risk`, `RISK_DOGBONES = True`, step 4b of `power.run`;
   `StageReport.data['risk_dogbones']`). S1 reserved a dog-bone only for pads the pour does not reach on the
   unrouted board. A pad in a fine-pitch row (two default-class tracks do not fit between neighbouring pads)
   that the pour reaches only on a signal layer loses that contact to its neighbours' escapes, and afterwards
   there is no room at the pad. Such a pad now gets its dog-bone in S1 when its net also has a plane (a pour on a
   layer signals do not use). Optional: a pad without a site is not reported as unreserved. Measured (staged
   pipeline): `cm5_minima` 23 open -> 13 (ground pads of the two 0.4 mm board-to-board connectors: 13 -> 2;
   116 such dog-bones), `tinytapeout` 46 -> 41, `one_air_max` 6 -> 7 (24 dog-bones; recovered by 5).
   This is the "hard reservation for pour pads whose only pour contact is one neck" the stage-2/3 notes asked
   for, limited to boards with a plane; 2-layer boards are unchanged.
5. **Pours pass, window search as second chance** (`engine._Run.pour_connect`, `POUR_WINDOW`,
   `POUR_WINDOW_REACH = 25 mm`). A pad-to-pour connection v1's search (board-wide grid) cannot close is given a
   copper target — the nearest pad of another cluster of the net — and `detail.route_conn` on the refilled
   board. The net's own plane layers are open to that search; tracks it lays ON its own plane are removed again
   when the stub + via alone hold the connection (`RouteState.connected` counts the fill island). Measured:
   `one_air_max` 7 open -> 0 (e.g. U5.22 beside U5.23 of the same net, 0.4 mm apart, which the grid search
   reported as walled in). No effect on `stickhub`, `coldfire`, `cm5_minima`.
   `power.place_dogbone(..., lanes=False)`: the pours pass no longer applies the exit-lane rule to dog-bones
   (the board is routed; the exact checker decides). No measured effect.
6. **`planx.v2_plan(suggested)`**: the suggested plan of `pcb_route_plan` as engine v2 takes it — engine
   switched and v1's automatic `skip: true` (ground net without a pour, "left for pcb_pour") dropped. Once that
   skip has been through a plan, `plan.resolve` marks it explicit and classify honours it, which is how GNDA
   stayed unrouted on `coldfire` (W5's known gap). The bench adapter calls `v2_plan` (`--opt keep_skips=true`
   restores the old behaviour); the MCP tool text does not mention it yet (W9's file).
   `coldfire`: GNDA 6 open -> 1.
7. **Budget**: `ALT_MAX_CONNS` 250 -> 150 (the second opinion no longer helps `cm5_minima`, 208 connections:
   staged 11 open, stage-1 pipeline 20; it doubled that board's wall time). Polish has a floor of its own share
   (the rescue rungs can use the budget up; twice polish ran with 0 units before this). Plane rescue, re-tree
   and plane ladder are floors too: a run can end up to 0.40 budgets over in work units on a board that uses
   all three.
8. **Tried and left off** (switches kept): `power.DECAP_LANE_CHECK` (refuse a decoupling track that closes the
   straight exit of a neighbouring pad — on `tinytapeout`'s 0.4 mm QFN the supply pads then stay open instead of
   their neighbours); a larger budget (150 s: `coldfire` +3, `tinytapeout` -9, `interf_u` -6 — the result is
   not monotone in budget across boards, only keep-best per stage is); the ladder before the fallback on
   `coldfire` (+2 connections for 4 M units).
9. **Unchanged interfaces**: additive only — `planx.V2Cfg.plane_rescue`, `planx.v2_plan`, `power.pour_at_risk`,
   `power.open_lanes` / `closed_lanes`, `place_dogbone(lanes=)`, engine passes named `planes`, `retree`,
   `planes-ladder` in `passes[]` / `stages[]`, adapter options `keep_skips`, `planes`.
10. **Not unit-tested**: plane rescue, re-tree, plane ladder and the pours-pass window search have no synthetic
    test of their own; they are covered by the engine's end-to-end tests only in so far as those boards trigger
    them (2-layer boards: re-tree only), and by the benchmark. `pour_at_risk` is covered by the corpus invariant
    in `tests/test_v2_power.py`; `plane_rescue` (field) and `v2_plan` by `tests/test_v2_planx.py`.

## Integration — build stages 4 + 5, second pass (pour pads: bridges, stitch vias, moving what is in the way)

Written by the second stage-4/5 integrator after the verifier's report on `v2_stage5` (9/14 clean, 19 pad-to-pour
failures without a certificate, over-reported `failed[]` on `coldfire`). Files touched: `v2/detail.py`,
`v2/engine.py`, `v2/judge.py`, `tests/test_v2_detail.py`. Nothing in §9 was renamed; everything is additive.
Result of record: `bench/route/results/v2_stage5b.md`.

What was measured first (`results/verify_4_5_1`): of 86 open connections 19 were pads of a poured net, all
reported as "walled in" by v1's pours search and nothing else. On `stickhub` (2 layers, GND poured on both) all 8
open connections were of this kind: 5 GND fill islands cut off by routed tracks, 3 `+5V` groups.

1. **Bridge mode of the window search** (`detail.route_conn(..., bridge=spec)`, `detail._bridge_side`,
   `_island_cells`, `_stitch`; `BRIDGE_MARGIN = 0.1`, `BRIDGE_GROW`, `BRIDGE_SPAN`, `STITCH_TRIES`). v1's pours
   search and the first pours-pass window search start at the pad. A bridge starts from ALL copper of the pad's
   side (its pads, tracks, vias and the inside of its fill islands, 0.1 mm inside the outline) and ends on ANY
   copper of the rest of the net. `spec = {'src': side, 'tgt': side, 'box', 'all'}` with
   `side = {'pads': [Terminal], 'segs', 'vias', 'islands': [(layer, points)]}`; the windows are round the nearest
   pair of points of the two sides (+2 mm, +5 mm), then round the whole source side. Uniform-cost search (the
   heuristic weight is 0), no pattern, no corridor; `conn.b` may be an open terminal.
   Before each search: **stitch** — a lattice point that is source copper on one layer and target copper on
   another is asked of the exact checker (`oracle.via_legal`; the window's via map is a raster with a guard band
   and misses sites a few cells wide), deepest inside both sides first, and one via is the bridge.
2. **Pours pass** (`engine._Run.pour_connect`, `POUR_BRIDGE`, `engine._bridge_spec`): after v1's search and the
   pad-to-pad window search, every pad still open gets a bridge, at the window pitch and once more at half of it.
   The sides come from `RouteState.net_groups` on the refilled board, so stubs laid earlier in the round count.
   `judge.POUR_ROUNDS` 2 -> 4: plain rounds continue while each one is accepted (a stitch via opens the way for
   the next; a round that adds nothing costs no KiCad call).
3. **Moving what is in the way** (`engine._Run.pour_rip`, a generator; `judge._rip_phase`; `POUR_RIP*`,
   `judge.POUR_RIPS = 8`). The refilled board is modelled a second time with this run's routed connections of
   unpoured nets, and the stubs of earlier pours rounds (groups of joined `conn == -1` pieces), as movable
   copper; everything else, the fill islands included, is static as KiCad left it. For a pad still cut off the
   bridge search runs with `soft_cost` there and names what its cheapest bridge crosses (at most 3 victims; a
   victim that cannot be moved is fixed and the search runs again, at most 4 times per pad). In the run's state
   the victims are ripped, the bridge is laid (checked against the run's own oracle), connections are re-routed
   (`detail.route_conn(widen=2, extra_vias=2)`, then `engine.reroute`); a proposal in which a victim, or any
   connection the re-route pushed aside, ends up unrouted is undone and never shown to KiCad. A yielded
   proposal is judged by KiCad as a whole: when the first check is not better, one plain pours round follows
   (re-connects a removed stub round the bridge, stitches what the moved copper cut off) and KiCad judges
   again; anything not better than the accepted state is restored. Its own budget share
   (`POUR_RIP_SHARE = 0.2`, a floor).
4. **Orphan copper** (`judge._check`, `judge._prune_orphans`). The worker's per-net unconnected figure is by pad
   groups; KiCad's ratsnest also lists copper joined to no pad (a stitch via whose island later copper
   reshaped). `chk['orphans']` = board total - sum over nets, added to `Score.opens`, so the judge's count is
   the harness's count; at the end of the pours pass such pieces are taken out when the board is not worse
   without them. Found on `stickhub`: the router reported 127/128 where kicad-cli counted 2 open.
5. **failed[]** (`judge.open_pour_pads`, `history['judge']['open_pads']`, `engine._failed`,
   `engine._bridge_diag`, `engine._diagnose`). A pad-to-pour record is listed only when the accepted, refilled
   board holds that pad apart from the rest of its net, one record per cut-off cluster (was: every failed
   pad-to-pour connection of a net that has anything open — 23 records for 20 open on `coldfire`). The record
   of a pad the bridge search could not join carries `blocked_by` / `mincut_nets` (the nets whose copper closes
   its side in), `static_share`, `probe`, `tried`, and `cut = {a, b, gap_mm, nets, at}` (how far the rest of
   the net is). A signal record without a named blocker (the ladder ran out of budget before a soft search)
   gets one priced search at the end (`DIAG_WORK = 80 000` units, at most 40 records, nothing is routed, not
   charged to a stage) that names the nets in the way.
6. **Measured** (`results/v2_stage5b`, serial, 2 runs, against `v2_stage5`): 2111 / 2186 = 96.6 % (was 2100),
   9 / 14 clean (unchanged: the gate's 10 is still missed), `stickhub` 8 open -> 2, `cm5_minima` 11 -> 8,
   `interf_u` 15 -> 14, `tinytapeout` 32 -> 31, `coldfire` 20 -> 20; nothing lower; 0 new DRC errors; 14 / 14
   identical twice. `cm5_minima` takes 188 / 182 s (design §1: 180 s): the rip phase's KiCad rounds.
   Stitch vias raise the via count (`phone_breathalyzer` 30 on common nets against 9 in the reference).
   `stickhub`: stitch vias and island bridges 8 -> 4 (4 plain rounds), moving connections / stubs 4 -> 2
   (3 of 7 proposals accepted by KiCad). Wall time on `stickhub` roughly doubles (KiCad judges each proposal,
   up to twice).
7. **What still stops `stickhub`** (2 open: `+5V` J3.1, GND C9.2). Both have a certificate: the bridge search
   is exhaustive at 0.0625 mm in its windows and finds no legal track or via; every bridge that crosses routed
   copper was tried with its victims re-routed, and either a victim had no other way or KiCad counted more
   open connections afterwards. The pours-pass stubs of `+5V` (21 connections on this board) are laid after
   the signals and one at a time; a human routes this net first and wide. Routing a poured net whose pour
   joins few of its pads together with the signals (S0 already gives it role `power` in the staged pipeline,
   which leaves 10 open here for other reasons) is the open lever.
8. **Not unit-tested**: `pour_rip`, `_rip_phase`, `_prune_orphans`, `open_pour_pads`, `_diagnose` (benchmark and
   the engine's end-to-end tests only). `tests/test_v2_detail.py` covers the bridge ends (`_bridge_side`,
   `_island_cells` margin, stitch candidates) on synthetic boards.
9. **Unchanged from the first pass and still true**: the second opinion (`ALT_MAX_CONNS = 150`) is what keeps
   `phone_breathalyzer`, `synth_qfp_fanout` (staged pipeline: 1 open) and `stickhub` (staged: 10 open) where
   they are; plane rescue is on by default.

## Integration — build stages 4 + 5, third pass (local fills routed with the signals; the ladder on v1-made trees)

Written by the third stage-4/5 integrator after the verifier's report on `v2_stage5b` (9/14 clean; `stickhub` 2 open,
both pour pads). Files touched: `v2/engine.py`, `v2/repair.py`, `tests/test_v2_engine.py`, `tests/test_v2_repair.py`.
Nothing in §9 was renamed; everything is additive. Result of record: `bench/route/results/v2_stage5c.md`.

What was measured first (`results/verify_4_5_2`, `stickhub`): `+5V` has 35 pads and a pour on B.Cu that joins 10 of
them (22 pieces on the unrouted board), `+1V8` 8 pads in 7 pieces. v1's planner — the minimal classify of the second
opinion — leaves every poured net to the pours pass, so 27 real connections were laid one stub at a time AFTER the
99 signals. The human reference routes `+5V` as 88 mm of track. The two pads left open were casualties of that
order (the router's own `suggest[]` said so: "route +5V before them").

1. **Local fill = routed net** (`engine.local_fill_nets`, `minimal_connections(local_fill=True)`,
   `LOCAL_FILL_SHARE = 0.5`, `LOCAL_FILL_MIN_CLUSTERS = 3`; `x.local_fill`, COPPER_V2_LOCAL_FILL=1). A poured net
   whose largest pour-joined cluster holds less than half of its pads, in at least 3 pieces, with every pour layer
   a layer its signals may use (no plane of its own), is planned like an unpoured net: v1's MST over its pad
   clusters, pad to pad, routed in the signals pass. (v1's planner decides on `Net.has_pour`; the flag is lowered
   for the call and put back — no v1 file is touched.) What the search leaves open of such a net still goes to
   the pours pass, which plans from the refilled board. Only the second opinion sets it (`x_alt.local_fill`);
   build stage 1 as gated (`--opt stage1=true`) and the staged pipeline (full classify: role `power`) are
   unchanged. `failed[]`: a pad-to-pad connection of a poured net is not listed by itself once the board is
   judged — the pours pass's per-cluster records (`open_pads`) are the exact list.
   Corpus nets that qualify: `stickhub` `+5V`, `+1V8`; `multichannel_mixer` `vbias`; three small nets on
   `one_air_max` (the latter two boards are above `ALT_MAX_CONNS`, so nothing changes there).
   Measured on `stickhub` (second-opinion pipeline alone): 2 open in 52 s -> 1 open in 18 s (the pours pass has
   17 pads to serve instead of 43); the one left was a signal, see 2.
2. **The ladder on trees the v1 search laid** (`repair.py`). The one connection left (`Net-(U1-EXT_RST#)`
   U1.40 - R3.2) needs three connections re-laid the way the human has them (SENSE between the two pads of R3,
   `+3.3V` off R3.1 another way). The ladder found the victim and the cluster and then gave up for two reasons
   that are properties of v1-made copper, not of this board:
   Both rules below apply only where the run says its trees are v1-made (`x.v1_trees`, set by the second
   opinion; `repair._v1_trees`; COPPER_V2_V1_TREES=1). Applied to every run they cost the staged pipeline
   connections (measured, `results/c_full2`: `cm5_minima` 8 -> 10 open, `interf_u` 14 -> 15, `one_air_max` 0 -> 1).
   * **Lazy cascade** (`LAZY_CASCADE`, `_lazy`, `_still_connected`). `_cascade` ripped every routed connection of
     a victim's net whose two NAMED pads were no longer joined. v1 joins clusters, not named pads, so a
     connection's pads are often joined through another connection's copper: ripping one `+3.3V` connection took
     four untouched ones along (7 mm away), the cluster grew past `CLUSTER_MAX` and the permutation returned
     without a word. With v1 trees, dependents are looked at AFTER the rip's re-routes and re-routed only when
     they are still apart then (their own copper stays where it is in the meantime: same-net copper the re-route
     may tee onto); `_still_connected` then also accepts KiCad's join rule (`connected(loose=True)`).
   * **Bridge rung** (`BRIDGE_RUNG`, `_bridging`, `repair._bridge`, `repair._last_resort`,
     `engine._bridge_spec(to=cluster)`). A victim or cluster member whose re-route fails pad to pad is tried as a
     bridge between the two parts of the tree it is there to join: from any copper joined to terminal a to any
     copper joined to terminal b (the detail stage's bridge mode, until now used by the pours pass only). The
     named pad was closed in by its own net's neighbours while the tree it hangs on had a free way further on.
   * Tried and left off: `VICTIM_FINE` / `PERM_FINE` (half and quarter window pitch as a further last resort):
     not needed on `stickhub`, and on `cm5_minima` the extra work cost 2 connections. `TAIL_MAX` (eager mode:
     tee-dependents a cluster has no room for, routed as a tail after each order; 0 = give the cluster up, as
     before, now with a log entry): at 8 `interf_u` lost one connection.
     COPPER_V2_REPAIR='NAME=0,...' sets the boolean switches for A/B runs.
   Measured: `stickhub` second opinion 1 open -> 0 (one permutation, first order: SENSE between the pads of R3,
   `EXT_RST#` straight, `+3.3V` bridged — the human's arrangement).
3. **Tried and left off**: `engine.LADDER_FLOOR` (a guaranteed ladder share on a board without plane layers when
   the fallback used the search budget up — `interf_u` is the one board): 0 of 7 rips accepted, 0 repaired, and
   the judge's path changed by one connection (14 -> 15 open).
4. **Tests**: `test_v2_engine.py` `test_local_fill_net_is_planned_with_the_signals`,
   `test_bridge_spec_between_the_two_parts_of_a_tree`; `test_v2_repair.py`
   `test_connection_joined_through_a_victim_keeps_its_copper` (fails with `LAZY_CASCADE = False`). The bridge rung
   inside the ladder has no synthetic test (its spec has; the rung itself: benchmark only).
5. **Measured** (`results/v2_stage5c`, serial, 2 runs, against `v2_stage5b`): see design §12. `stickhub` 2 open
   -> 0 (the tenth complete and clean board), every other board item for item what it was.
6. **Still true**: the gate's `synth_qfp_fanout` 100 % and `stickhub` 100 % are the second opinion's results (staged
   pipeline: 1 and 10 open); `ALT_MAX_CONNS = 150` decides which boards get one.

## Integration — build stages 6 + 7 (via-frugal routing, polish, steering demo), as integrated

Written by the stage-6/7 integrator. Start state: the tree of `v2_stage5c` (all 20 `tests/test_v2_*.py`,
`test_guided_route.py`, `test_pcb.py` green; no partial edits found). Files touched: `v2/engine.py`,
`v2/detail.py`, `v2/polish.py`, `v2/judge.py`, `v2/planx.py`, `tests/test_v2_detail.py`, `tests/test_v2_engine.py`,
`tests/test_v2_planx.py`, new `bench/route/steer_demo.py`. No v1 file, no harness / scoring file. Nothing in §9 was
renamed; everything is additive. Result of record: `bench/route/results/v2_stage7.md`.
**The default engine is NOT flipped** (`planx.DEFAULT_ENGINE = "guided"`): the final gate is missed (point 7).

What was measured first (`results/v2_stage5c`): vias on common nets against the human reference, per board —
`phone_breathalyzer` 30 / 9, `coldfire` 405 / 179, `sonde_xilinx` 7 / 3, `complex_hierarchy` 5 / 0,
`multichannel_mixer` 44 / 29. On the small boards the cause is the price: at `via_cost` 5 mm the search hops
layers where the human detours on one layer. On `coldfire` the cause is the layer set: the human routes signals on
all four layers (In1 / In2 carry most of the buses), the suggested plan keeps v2 on the two outer ones.

1. **Via-frugal run** (`engine._frugal`, `VIA_FRUGAL = 3.0`, `VIA_FRUGAL_FAILS = 0.03`, `VIA_FRUGAL_FAILS_MIN = 2`;
   plan field `global.via_frugal` (default true), COPPER_V2_FRUGAL=0). Before the normal run the whole staged
   pipeline runs once with every via cost of the plan x 3 (`cfg.via_cost` and each `NetCfg.via_cost`, put back
   afterwards). Its result is returned only when the board proves easy: no plane layers, the frugal detail
   stage leaves at most max(2, 3 %) pad-to-pad connections without a way (`detail.run(max_failed=)` stops at
   that count: `data['hard_failed']`, `data['aborted']`), and the run ends complete, with no `failed[]` and no
   new DRC error. Otherwise the attempt is dropped (`_NotEasy` / result discarded, scratch board and
   `cfg.regions` restored) and the normal run starts from a fresh `_Run`: its copper is item for item what it
   was without this step (checked on `one_air_max`; every not-easy board of `v2_stage7` that nothing else
   touches has the copper of `v2_stage5c`). Why not a plain higher via cost: measured with
   `plan.global.via_cost = 15` on 8 boards (`results/vc15`) the easy boards lose 30 - 100 % of their vias,
   `coldfire` goes 20 -> 28 open and `interf_u` 14 -> 23. Kept on: `ecc83`, `sonde_xilinx`,
   `phone_breathalyzer`, `complex_hierarchy`, `synth_bus_cross`, `multichannel_mixer`. Cost where it is not
   kept: setup + power + fan-out + global + the part of detail up to the abort (boards without planes only):
   `interf_u` about 10 s, `stickhub` 5 - 8 s; `synth_bus_cross` takes 15 s instead of 6 s (the frugal search
   is the slower one there).
2. **Via-frugal re-route** (`polish.revia`, engine pass `vias` before polish; `REVIA_*`, `engine.REVIA`,
   COPPER_V2_REVIA=0). Design §4.10 pass 4 as a window search: a routed connection with vias is ripped and
   re-laid with `detail.route_conn(via_limit=n - 1, via_scale=4, lateral=...)` — one rung, a window without
   corridor mask grown on every side by the room a path 1.3 x as long can use. Kept (one transaction) when it
   has fewer vias, is at most 1.3 x / 3 mm per saved via longer, every connection of the net that was joined
   is still joined, and `state.score()` is better. New `route_conn` keywords: `via_limit`, `via_scale`,
   `rungs_max`, `lateral`. Limits found by measurement:
   * poured nets are not touched: their vias are their way to the pour (`one_air_max`: 13 vias saved, then 3
     pour pads lost in the pours pass);
   * it runs only when no pad-to-pad connection is open and only inside what the budget still holds: on the
     four largest boards it saved 2 - 3 vias for 5 - 10 s each (a connection cannot be re-laid without moving
     its neighbours there);
   * the ladder's own windows are too narrow for the detour a human takes (5 mm + 25 % to the side): hence
     `lateral`.
   Effect where it runs: `pic_programmer` 6 -> 2 vias on common nets, `phone_breathalyzer` 20 -> 16 after the
   frugal run, `synth_qfp_fanout` 48 -> 41 in total.
3. **Pours pass** (`judge.pours_pass`): a give-room re-route that by itself makes new DRC errors is undone at
   once. Before, stubs were laid on top of it and three more commit + DRC rounds followed, all undone
   (`cm5_minima`: 13 -> 10 KiCad round trips, same copper).
4. **Steering demo** (`bench/route/steer_demo.py BOARD --rule order|first|budget --base NAME`): run 1 with the
   suggested plan, one plan patch built mechanically from run 1's `suggest[]`, run 2 with it, KiCad's verdict
   on both (`results/steer_<rule>_<board>.demo.json`). Measured on `coldfire` from `v2_stage7`:
   * `--rule order` (the four `{"order": [net, "*"]}` patches merged: `/DDAT1`, `/DDAT3`, `/UTXD1`, `GND`
     first): 20 open -> **14** (95.8 % -> 97.1 %), 0 new DRC, identical twice; vias 497 -> 585.
   * `--rule first` (`suggest[0]`: `time_budget_s: 180`): 20 -> 17 open, 0 new DRC (one run; it takes about
     twice as long).
   The same rules do NOT help the other three failing boards (single runs, on the tree before the last
   edits, run in parallel): `interf_u` order 14 -> 23, `tinytapeout` order 31 -> 39 and budget 31 -> 38,
   `cm5_minima` order 8 -> 11; `suggest[0]` on `tinytapeout` (`fanout U6 via`) 31 -> 35, on `interf_u` (a
   capacity region) 14 -> 16, on `cm5_minima` no change. `suggest[]` is advice a caller has to try, not a
   guarantee, and the result is not monotone in the budget (as noted for stage 5). A plan that opens all four
   layers on `coldfire` (`global.layers = [Top_layer, In1.Cu, In2.Cu, B.Cu]`, not something `suggest[]`
   offers) leaves the same 20 open with 391 vias instead of 497.
5. **Looked at**: `phone_breathalyzer` routed and reference pictures, `coldfire` routed and reference pictures,
   one global-sketch overlay and one failure crop made by `test_v2_diag` (both legible: the cut is labelled,
   blocking nets in orange, the missing connection in yellow).
6. **Tests**: `test_v2_detail.py` `test_via_limit_and_the_via_frugal_reroute`, `test_run_stops_after_max_failed`;
   `test_v2_engine.py` `test_via_frugal_run_is_kept_only_on_an_easy_board`; `test_v2_planx.py` (`via_frugal`
   field). The early undo of the give-room round has no synthetic test (benchmark: `cm5_minima`).
7. **Final gate, as measured** (`v2_stage7`, serial, 2 runs; the machine was shared with another team's
   benchmark, wall times moved by up to 50 % between the two runs of one board):
   * done 2113 / 2186 = 96.7 % (target 97 %): **missed** by 8 connections;
   * 10 / 14 complete and clean (target 11): **missed**;
   * 0 new DRC; no board below baseline; 14 / 14 identical twice; odd-angle <= 1.2 %: met;
   * vias on common nets: 1562 / 1481 = 1.05 x over the corpus; per board <= 1.25 x on 7 of the 10 boards
     where the ratio is defined, **missed** on `coldfire` 2.26 x (405 / 179), `phone_breathalyzer` 1.78 x
     (16 / 9; was 3.3 x), `sonde_xilinx` 1.67 x (5 / 3; was 2.3 x);
   * wall time <= 180 s: **missed** on `cm5_minima` (181.5 / 183.1 s; 172 s alone at a quieter moment, 210 s
     under load);
   * segments < 0.2 mm (§1: < 3 %): above 3 % on 9 boards (`stickhub` 30.7 %) — polish got no new pass for
     them.
8. **Not done / unverified**: `mcp/catalogue.py` and `mcp/compact.py` still do not know the v2 result keys;
   `tests/test_tokens.py` fails 2 of 29 on the catalogue size (58 799 B > 58 000 B): the slim texts of
   `fab_export` and `sch_tidy` are stale after the other team's edits, no route tool text was changed here;
   bus lateral ordering (design §12 stage 7 row) was not built; `coldfire` at 600 s was not re-run; the frugal
   run has no budget-monotonicity test; the steering numbers of the three boards that did not improve are
   single parallel runs.

## Integration — build stages 6 + 7, second pass (oracle: pad clearance on holes; via-frugal ladder; what was measured and left off)

Written by the second stage-6/7 integrator. Start state: the tree of `v2_stage7` (all 20 `tests/test_v2_*.py`,
`test_guided_route.py`, `test_pcb.py` green; no partial edits). Files touched: `v2/oracle.py`, `v2/engine.py`,
`v2/detail.py`, `tests/test_v2_oracle.py`, `tests/test_v2_engine.py`. No v1 file, no harness / scoring file.
Result of record: `bench/route/results/v2_stage7b.md`. **The default engine is still NOT flipped**
(`planx.DEFAULT_ENGINE = "guided"`): the final gate is missed (point 6).

1. **Oracle: a pad's clearance override reaches its hole** (`Oracle._hole_override`, `_need` for `cls == 'hole'`).
   This is W1's finding 17 / W6's known gap: KiCad reports `hole_clearance (pad clearance 1.7 mm; actual 0.97 mm)`
   for copper next to an NPTH pad that carries a local clearance, the oracle used the board's hole clearance. The
   need for a hole shape is now `max(board hole clearance, the pad's / footprint's clearance override)`. Every
   search reads `oracle.need` (windows) or is checked by it at emit time (legacy), so no other file changed.
   Effect on `cm5_minima` (the only corpus board with such pads): the judge's round 0 has 0 new errors where it
   had 5, the give-room round 0 where it had 17, the rejected rip rounds are gone: 6 commit + DRC round trips
   instead of 10, wall time 181 s -> 146 s in the serial run. The copper differs, and the board ends with 9 open
   connections where it had 8 (see point 4 on how much a changed input moves these boards).
   Test: `test_v2_oracle.py::test_pad_clearance_override_applies_to_its_hole`.
2. **Via-frugal ladder** (`engine.VIA_FRUGAL_LADDER = (3.0, 8.0)`, `_frugal_ladder`, COPPER_V2_FRUGAL_X="3,8").
   The via-frugal run of the first pass is tried at each multiple in turn while each ends complete and clean; the
   candidate with the fewest vias in KiCad's count (then the shorter copper, then the lower multiple) is returned.
   A board that is not easy at 3 x pays for that one trial only, exactly as before (`interf_u`: 108 s as before;
   an 8-first order cost it 45 s more and is why the order is cheapest first). Kept at 8 x: `sonde_xilinx` 5 -> 2
   vias (human 3), `multichannel_mixer` 27 -> 25 (human 29). `phone_breathalyzer` stays at 16 / 9 on common nets:
   its 8 x candidate has more vias (27 in all against 19), because what the dearer via saves in the detail stage
   the pours pass and the fallback put back.
3. **Automatic layer directions in the detail stage — built, measured, left OFF** (`detail.AUTO_DIR_BIAS = 0`,
   COPPER_V2_AUTODIR). What prompted it: the `tinytapeout` reference is strictly horizontal on F.Cu / vertical on
   B.Cu, the global stage computes such directions (`auto.layer_dirs`) and the window search did not price them
   (only `plan.global.layer_directions` reached `detail._moves`). Open connections by bias (single runs, boards
   run in parallel):

   | bias | interf_u | tinytapeout | cm5_minima | coldfire | one_air_max |
   |---|---|---|---|---|---|
   | 0 (default) | 14 | 31 | 9 | 20 | 0 |
   | 0.3 | 21 | 32 | 5 | 15 | 1 |
   | 0.5 | 11 | 32 | 7 | 19 | 2 |
   | 1.0 | 19 | 32 | 3 | 16 | not run |

   No bias wins on every board, the sums are 74 / 74 / 71 / >= 70, and every non-zero bias costs `one_air_max`
   its clean result. Not a default.
4. **What the table in point 3 says about the remaining 74 connections.** A change that should be neutral or
   mildly helpful moves each hard board by 3 - 7 connections in either direction (so does the oracle fix of
   point 1: `cm5_minima` 8 -> 9; so did the first pass's 150 s budget and `order` patches). The staged pipeline is
   sequential — window search in rank order, per-connection fallback, a ladder that accepts few rips — and its
   result on a congested board is one draw from a wide distribution, not a converged negotiation. Picking the
   best draw per board from this corpus would be fitting to it; a portfolio of draws inside one run does not fit
   in 180 s on these boards (110 - 150 s each).
5. **Fan-out halo — built, measured, left OFF** (`engine.halos`, `HALO_MM = 0`, COPPER_V2_HALO="mm,cost"). A cost
   region round every part with >= 8 escapes for nets without a pad on it (plan regions: read by the global
   stage, the window search and v1's search). Prompted by the `tinytapeout` picture: round U6 (QFN-56, 0.4 mm)
   the escape stubs end among vias and tracks of nets that only pass by. Measured at 2 mm / 3 x together with
   bias 0.5: `tinytapeout` 32 -> 38, `cm5_minima` 7 -> 5, `interf_u` and `coldfire` unchanged. Not a default.
6. **Final gate, as measured** (`v2_stage7b`, serial, 2 runs; `python bench/route/run.py --router v2 --name
   v2_stage7b --runs 2`): 2112 / 2186 = 96.6 % (target 97 %: **missed**, 9 short; one fewer than `v2_stage7`,
   the `cm5_minima` connection of point 1); 10 / 14 complete and clean (target 11: **missed**); 0 new DRC; no
   board below baseline; 14 / 14 identical twice; odd-angle <= 1.2 %; wall time <= 146.5 s on every board (**met**
   now: `cm5_minima` 146.5 / 142.2 s, was 181.5 / 183.1 s); vias on common nets <= 1.25 x: **missed** on
   `coldfire` 2.26 x (405 / 179), `cm5_minima` 1.90 x (272 / 143; it read 0.89 x in `v2_stage7` only because GND
   and its dog-bone vias were then among the common nets — GND has one pad open now and drops out of the
   comparison), `phone_breathalyzer` 1.78 x (16 / 9); `sonde_xilinx` is at 0.67 x (was 1.67 x). Steering demo
   (`steer_demo.py coldfire --rule order --base v2_stage7b --name steer7b --runs 2`): 20 -> 14 open, 0 new DRC,
   identical twice, vias 497 -> 585, 170 s. Why the missed criteria are not reachable by tuning this tree:
   * **11 boards clean**: the four open boards have 14 / 31 / 9 / 20 connections open and none came closer than
     3 open in any variant run (point 3).
   * **`coldfire` vias 2.26 x**: the human routing has 34 % of its track length on In1.Cu / In2.Cu (28 % / 6 %),
     layers that carry the GND / +3.3V pours; the suggested plan keeps the router on the two outer layers and v2
     crosses a plane only for a connection that failed everywhere else (plane rescue). With all four layers open
     (first pass, point 4) the board still has 391 vias against the reference's 253. Reaching 1.25 x needs bus
     bundles laid planar on an inner layer, i.e. cutting planes by default, which design §0 excludes.
   * **`phone_breathalyzer` vias 1.78 x** (16 / 9): the reference is "whatever routing is on that board"
     (bench README); see point 2.
   * **`tinytapeout`**: 19 of 31 open connections end on a U6 pad whose escape is boxed in (picture:
     `bench/route/.work/int67b/logs/tt_u6.png`). The fan-out stage reports them escaped; the escapes are later
     surrounded. This needs via-in-ring fan-out with the escape region reserved, not a parameter.
7. **Profile of `cm5_minima`** (cProfile, one run): 37 % of the time is `window.build` (787 windows; the span
   rasterisers are Python generators, one resumption per row and shape), 23 % the window search, 19 % KiCad
   round trips. A faster window build would shorten every board without changing any copper (budgets are work
   units); not done.
8. **Not done / unverified**: everything in point 8 of the first pass still stands (`mcp/catalogue.py`,
   `mcp/compact.py`, `tests/test_tokens.py`, bus lateral ordering, `coldfire` at 600 s). The two switches of
   points 3 and 5 have no unit test (they are off). The steering demo was re-run on `coldfire` only.

## v2 is the default (owner decision, 2026-11-11) — what the default has to guarantee

`planx.DEFAULT_ENGINE = "v2"`; `plan.global.engine: "guided"` selects v1. The statements further up that the
default "is not flipped" describe the tree before this date. The safety contracts v1's tests pin are product
requirements of the tool, not of an engine; where each one lives for v2:

1. **Forbidden areas** (`regions[].forbid`, `nets[..].avoid`, strict corridors) are rules of the exact checker:
   `Oracle.set_plan(cfg)` (called in `_Run.setup` and for the pours pass's oracle), `Oracle.plan_hit(net, layer,
   x1, y1, x2, y2)`, and `seg_ok / seg_legal / via_ok / via_legal` reject on it (a `Blocker` with rule
   `"keepout"` and tag `"plan region '<name>'"` / `"plan avoid rectangle of <net>"`). The router's own feedback
   regions (`idx >= oracle.PLAN_FIRST_AUTO`: judge, halos) are costs for the searches, not rules. A via counts
   on every layer, also for a layer-limited region. `_Run.plan_audit()` checks every piece of the state once more
   before the judge (a stage that calls `RouteState.add` without asking — `add` does not check — loses that
   connection, reported in `failed` as "routed through plan region ..."); if the judge's own re-routes leave an
   offending piece, the run is returned unjudged (`not_committed`) and the session writes nothing.
   `_Run._plan_reason` words a failure whose pad lies inside a forbidden area.
2. **dry_run / compare / bad plan**: `plan.resolve` validates before anything is touched; the engine only ever
   writes the scratch copy; `session._route_v2` replaces the board file once (`.copper-tmp` + `replace`), only when
   `judged and not dry_run and routed > 0 and new copper`.
3. **Nothing routed -> nothing written**: `_route_v2` discards a judged run that closed no connection (fan-out
   stubs, reserved vias, a rip-up) and also a rip-up after which the judge never ran (every connection failed in
   the search, nothing to commit — `ripup='nets'` on a board whose pours need no work was the case that still
   reported `ripped_up: 18`). Result: `written: false`, `ripped_up: 0`, `not_written`, no `previous`, no `drc`.
4. **Ground left for the pour**: the plan's automatic `skip` (ground, 6+ pads, no pour) is honoured by classify;
   `skipped` comes from classify's report (`_Run._skipped`), with v1's entry shape `{net, connections, reason}`.
   `planx.v2_plan(suggested)` (the benchmark's plan) still drops that skip on purpose.
5. **Result size**: `route_tools._compact_v2` is the default (`stages`, `judge`, `portfolio` out; `auto` /
   `congestion` only with failures, cuts or a dry run; notes / critique / suggest capped; `more` names what
   `detail=true` adds). `tests/test_tokens.py` caps any single result at 6000 B.
6. **Tests under both engines**: `tests/test_guided_route.py::test_e2e_safety_contracts_hold_under_both_engines`
   (real board, KiCad as judge) and `tests/test_v2_engine.py::
   test_plan_forbidden_areas_are_rules_of_the_exact_checker_and_audited_before_kicad` (scripted stage that never
   asks the checker).
7. **Ground left for the pour, then `pcb_pour`** (the 18-call replay of `tests/test_tokens.py`): the pour does
   not exist while the router runs, so nothing kept the future thermal reliefs free and a track through a spoke
   of J2.2 became a `starved_thermal` error after `pcb_pour` (v1's copper happened not to). `power.left_for_pour(cfg,
   net)` (the plan's own rule skipped it, reason "... left for pcb_pour") and `power.thermal_spokes(board, pad,
   future=True)`: S1 registers the soft spoke corridors of such a net's pads with a default pour's values (0.5 mm
   gap and spoke) on every copper layer of the pad, and books them like those of an existing pour. Not done for a
   net the plan skips itself (`skip: true`) or routes as tracks (`skip: false`), nor for `nets=[...]` runs that
   do not select the ground (its reason is then "not selected").
8. **Idle escape vias**: `_Run.drop_idle_escapes()` (before the plan audit and the judge) removes a `stage ==
   "fanout"` via that has copper of its net on one layer only — the pad's connections left on the pad layer —
   unless removing it opens a routed connection (`RouteState.dangling` before / after). Vias of a poured net or
   one left for the pour, and vias within 1 mm of a through-hole pad of their net, stay. Polish does not touch
   protected pieces, so these reached KiCad as `via_dangling` warnings (2 on the replay board).
9. **Via-frugal trials need a search**: `_Run.run` raises `_NotEasy` at once when the board has no pad-to-pad
   connection (a routed board run again after `pcb_pour`: pour pads only). Before, each rung of the ladder ran the
   judge and the pours pass in full and was then thrown away (14.6 s -> 10.0 s for that call on the replay board;
   the classic router needs 9.1 s for it).
10. **Wall time through `board_build`** (17-part replay board, this machine): the first `pcb_route_guided` is
   17 - 20 s under v2 (two via-frugal rungs, each a full pipeline + KiCad round trip) against 10 s under v1, so
   `board_build` reaches its 45 s time box just before `verify` and is resumed once. `flow_tools.ESTIMATE_S["route"]`
   is still v1's 12 s; raising it changes whether the after-pour route starts and was left for the owner.
11. **`verify` `next`** was prose ("fix DRC errors (see findings[].fix)") for a connected board with DRC errors;
   v1's flow never got there because one GND pad always stayed open ("pcb_route() - 1 unrouted"). The replay
   board carries 12 `drill_out_of_range` errors of the ESP32 module's 0.2 mm thermal vias from placement on,
   under either engine. `verify_tools._drc_next` returns a call.

## Electrical constraints, step A (design §14: model, plan syntax, detection, measurement, reporting, bench) — as built

Engineer A of three (2026-11-11). **No copper is laid, moved or removed by anything in this step**: the router
measures and reports. Length tuning (step B) and coupled pairs / width rules / hard `max_length` (step C) are not
built. Files: new `v2/constraints.py`, `tests/test_v2_constraints.py`, `bench/route/cons.py`; edits in
`v2/planx.py`, `v2/engine.py`, `v2/classify.py`, `v2/judge.py`, `guided/plan.py` (alias hint only),
`guided/session.py` (one key passed through), `tools/route_tools.py`, `bench/route/{prepare,run,common}.py`,
`bench/route/routers/v2.py`. Results: `bench/route/results/cons_before.md` (A0), `cons_A.md`, `cons_A_off.md`,
`constraints_baseline.md`.

**Where things live (B and C read this first)**

1. `planx.V2Cfg.cons_plan` — the plan's declarations, validated, plain data (`planx.cons_plan_default()` documents
   the keys: `mode, groups[{name, nets, tol, len, amp}], off, no_groups, pairs{p: {n, gap, width, skew, uncoupled,
   coupled, z}}, pairs_given, widths{net: ((layer, mm), ...)}, z{net: ohm}`). `V2Cfg.cons` — the resolved
   `constraints.Constraints`, set by `engine._Run.constraints()` in `setup()` **before** classify; `V2Cfg.cons_env` =
   `{stack, die, use_height, tuning}` (the stackup, pad-to-die lengths, KiCad's `use_height_for_length_calcs` and
   the project's `tuning_pattern_settings` defaults — B's amplitude / spacing defaults are `cons_env["tuning"]
   ["single"]`). A deep copy of `x` (via-frugal rung, second opinion) carries all three; `cons` is resolved once
   per plan object. The engine's module lookup is `_module("constraints")`: a test that scripts the modules must
   pass `constraints="real"` to get any of this (tests/test_v2_engine.py does).
2. `Constraints.tuned()` = groups that may get copper (`LenGroup.mode == "tune"` **and** plan mode `auto | plan`);
   `Constraints.coupled_pairs()` likewise for `Pair.coupled`. Use these two, not the raw tuples: `report` mode and
   name-only groups are filtered there. `group_of / pair_of / width_on` as in §14.2.
3. `LenGroup` semantics as built (the dataclass fields are §14.2's; the encoding of the three checks is new):
   `tol > 0, target None` → every member `>= longest − tol` (KiCad skew); `tol > 0, target` → `target ± tol`;
   `lo / hi` → absolute window of a KiCad `length (min)(max)` rule. **`tol == 0` marks a pure window group**
   (then only `lo / hi` count and `target` is the rule's `opt`). A skew rule and a length rule over the same nets
   become ONE group with both checks. `constraints.window(group, {net: length})` returns `(low, high, reference)`
   — the tuner should aim with it rather than re-derive it.
4. `constraints.net_length(ctx, net) -> LenParts(track, inpad, via, die, total, vias, branched)` and the pure
   `measure_net(segs, vias, pads, stack, use_height, die)`; `net_copper(ctx, net)` = this run's pieces +
   `board.kept_tracks / kept_vias`. `coupled_stats(segs_p, segs_n, width, gap, gap_lo, gap_hi)` is here, not in
   `pairs.py` (step A needs it for the report; C may move or extend it). `kicad_markers(raw)` parses the five
   marker types; `measure(ctx, kicad=check)` builds `out["constraints"]`.
5. `judge._check` returns two more keys: `chk["cons"]` (= `kicad_markers(raw)`: counts, per-net types, KiCad's own
   lengths) and `chk["cons_new"]` (how many NEW markers were constraint types). Those markers are removed from
   `chk["new"]`: they are never blamed, never in `Score.new_errors`, never cause a re-route or a removal. B's
   `tune_pass` gets KiCad's "actual" per net from `chk["cons"]["len"]`.
6. Result: `out["constraints"]` (absent without constraints), `out["auto"]["constraints"]` (the detection listing),
   `out["drc"]["new_errors"]` = copper + constraint errors (KiCad's truth), `out["drc"]["constraint_errors"]` (only
   when non-zero). `engine._key(out)` subtracts the constraint errors: a board that is complete with clean copper
   but unmet constraints still counts as `(0, 0)` for the via-frugal ladder and does not trigger the second
   opinion — without that, every board with an unmet rule would be routed twice for nothing. B3's "third element
   = miss count" is not done.
7. `classify.TUNED_PRIORITY = 70`: members of `cons.tuned()` groups get `priority = max(own, 70)` (§14.6) — **only
   once the tuner exists** (`classify._tuner_built()`: `v2/tune.py` importable and not a stub). Reason, measured:
   with the rule on and no tuner, `cons_bus_match` routed `/SD_D2` 10 mm longer with 2 vias (170.81 mm / 2 vias
   against 160.95 mm / 0 vias in all) and the group's spread did not move (13.4 mm either way). So in step A the
   routing ORDER and the copper of every board are exactly what they were; the rule switches itself on when B
   adds `tune.py`. **B: check it on `cons_bus_match` before keeping it** — if tuning the short members is easier
   than un-detouring a long one, a lower priority for the group (or none) may serve the group better.
   `results/cons_A` and `results/cons_A_off` (`global.constraints: "off"`) therefore carry identical copper.

**Measured in KiCad 10.0 beyond §14.9** (`bench/route/cons_probe.py midpad viastub` re-measures items 8 – 10 and
prints the expected figure beside KiCad's; the facts are pinned by unit tests in `tests/test_v2_constraints.py`):

8. Two chains that end on the SAME pad centre on one layer (a pad in the middle of a daisy chain, each side with
   a dogleg inside the pad): KiCad reports the plain sum (30.3234 mm), no shortcut. So the in-pad shortcut applies
   only where **exactly one** segment end sits on the pad position on that layer. (Three or more ends: not
   measured; the model applies no shortcut.)
9. **Vias get the same shortcut as pads.** A track that turns `s` mm before its via, inside the via's copper circle
   (radius r), is counted as the straight line from where it enters the circle to the via centre: `s + sqrt(r² −
   s²) − r` shorter (measured for s = 0.01 … 0.2 mm on a 0.6 mm via: 21.0585, 21.0426, 21.0342, 21.0126, 21.0055;
   s = r = 0.3 and s = 0.5: counted in full; confirmed on a second, hand-made geometry by the `viastub` probe:
   33.1624, 33.1797, 33.2464, 33.4824, 33.7216 predicted and reported). v2 lays such stubs (a 0.035 mm jog into a via on `cons_mixed`), and
   without this rule the model was 0.033 mm off there. The tuner must not place a bump corner inside a via pad.
10. A via in the middle of a straight track with a branch on another layer still counts both layers (34.58 mm =
    33.0 + 1.58); an inner-to-inner hop In1–In2 on the default 4-layer stack is 0.515 mm (half + dielectric +
    half), as §14.5 inferred.
11. `pcbnew.SaveBoard` writes its own `.kicad_pro` next to the board and overwrites one that is already there. A
    project file written by hand must go in **after** the board is saved, merged into KiCad's file
    (`prepare.prepare_cons` does that). The first build of the constraint boards wrote it before, and their net
    classes were silently the Default class until this was found. (`bench/route/cons_probe.py` writes its
    short-form `.kicad_pro` after `SaveBoard`; whether KiCad honours net classes from that short form was not
    checked — none of its measurements depends on a class value.)
12. Model against KiCad on the four constraint boards (`len_model_err`, 20 constrained nets in all): see
    `results/constraints_baseline.md`. Known remaining source of error: the worker's extract rounds positions to
    0.1 µm, so a pad that is not on a 0.1 µm grid (rotated footprints) gets a track end that is not exactly on its
    position; KiCad then applies no in-pad shortcut while the model does.

**Detection as built (differences from §14.4)**

13. Precedence plan > dru > class > name as designed. A plan that merely echoes `pcb_route_plan`'s suggestion (the
    same `p / n / gap / width`, no `skew / uncoupled / coupled / z`) keeps `src: "name"` — the benchmark sends the
    suggested plan back verbatim, and a guess must not become a declaration by a round trip. Same for the
    suggested `keep_short` of a crystal.
14. "Class" for a pair = its net class is not `Default` and its pair width / gap differ from the Default class's
    as the board extract carries them (`board.rules["default"]`), or the class name reads as an impedance
    (`90ohm`, `100R`, `Z50`, `50R0`). Single-ended nets of an impedance-named class get a `WidthRule(src="class")`
    — report only (`constraints.widths.off` lists stretches laid at another width: necked stretches on
    `cm5_minima` will show up there).
15. Name groups (`constraints.NAME_GROUPS`, `name_groups()`): SD / eMMC, QSPI (IOn + clock on any stem), RGMII
    tx / rx, DDR byte lanes (DQ + strobe + mask per lane of 8), DDR address (needs a DDR / SDRAM / MEM stem), ULPI,
    parallel video, and the generic "≥ 4 numbered lines with one alpha prefix + exactly one clock on one
    non-empty stem". Always `mode: "report"`. What is deliberately NOT detected is pinned in
    `test_name_groups_false_positives_are_not_detected` (numbered lines without a clock, a clock on another stem,
    bare `/D0../D7` + `/CLK`, two clocks, three members, plain SPI, address lines without a DDR stem).
16. KiCad conditions read: `A.NetName == '..'`, `A.NetClass == '..'`, `A.hasNetclass('..')`,
    `A.inDiffPair('..')` joined by `||`, each optionally `&& A.Type == / != '..'`; matching is KiCad's (`*`, `?`,
    case-insensitive; `inDiffPair` by `MatchDpSuffix` with or without the base's trailing underscore, partner must
    exist). Anything else (`B.`, `!=` on a net, two net tests and-ed, `intersectsArea`, `Layer`) → `unread`. A
    value in `ps / fs / ns` → `unread` + a note (time-domain rules). `(severity ignore)` and a project
    `rule_severities` entry `ignore` → listed in `notes`, not enforced. `track_width` counts as "ours" only with an
    `(opt …)`; a bare minimum stays `model.dru_minimums`' business.
17. `plan._fields` gained an alias table for the "did you mean" hint only (`planx.FIELD_ALIASES`: `tolerance →
    tol`, `target_mm → len`, `max_skew_mm → skew`, `impedance_ohm → z`, …): the owner section's long names are the
    ones a caller will try first, and difflib alone does not connect them.

**Tool layer**

18. `route_tools._cons_findings` emits ONE finding per code naming up to `CONS_FIND_MAX = 3` cases with their
    amounts ("6 length group(s) outside their window: bus1: /BUS1_D2 2 mm short of 42.2 (tol 0.5); … (+3 more in
    constraints)") — §14.10 implied one per case; with 12 groups + 12 pairs that alone was 5.7 kB. The fix texts
    say plainly that this build lays no meanders and couples no pair: **B and C must reword them** when that
    stops being true. `_compact_constraints`: misses first, `CONS_MAX = 6` rows per kind, 0.01 mm, soft keep-short
    rows and per-net figures only with `detail=true`; a missed pair row keeps the amount (`skew_over`,
    `uncoupled_over`) and drops the limit (value − amount). Measured: 12 groups + 12 pairs → 10.2 kB in full,
    4.2 kB for the whole default result, nothing cut (`tests/test_tokens.py::
    test_constraints_block_stays_inside_the_result_cap`). With every other finding kind on top (keep-short, width,
    guessed group, unread rule, unexplained KiCad error) the result is about 6.2 kB before the wire layer's 6 000 B
    cap, which then shortens the list of findings — never a constraint row (asserted in the same test).
19. `DRC_ERRORS_NEW` ("could not repair or attribute") counts `new_errors − constraint_errors`. The old info
    finding `PAIR_SKEW` (from `out["diff_pairs"]`, > 1 mm) is suppressed for a pair that has a skew limit in the
    constraints block; `out["diff_pairs"]` itself is untouched (C4 retires it).
20. tools/list is 57 994 B, unchanged to the byte; `SCHEMA_V2` grew 819 → 1 018 B (limit + 260);
    `plan.SCHEMA_TEXT` is 1 984 characters (its test caps it at 2 000: **16 characters left**). The documentation
    is `copper_help(tool="pcb_route_guided")` (`registry.add_help` at the bottom of `route_tools.py`).

**Benchmark**

21. `run.py --suite route | cons | all` (alias `--group`, `constraints` = `cons`; default `route`, so the 14-board
    table and its totals are what they were; a `--board` named explicitly runs whatever its suite).
    `summarize()` puts constraint boards in their own section and never into the totals. `results/<name>_off/`,
    when present, supplies the wall-time limit and the "done %" reference.
22. Boards as built differ from §14.11 in geometry only: `cons_bus_match` J1 pitch is 2.0 mm (1.27 gave a natural
    spread of 5.5 mm, below the asserted 6); `cons_pairs_cross` has its connectors 0.6 mm from the board edge (at
    3 mm the router went round behind them with 0 vias — the pairs did not have to cross); `cons_usb_pair`'s wide
    net is `/DRV` in class `WIDE` (0.5 mm).
23. Tests that enumerate `bench/route/corpus` (`test_v2_fanout`, `test_v2_globalroute` in their full modes) now see
    four more directories.

**Not done / not verified in step A**

- No tuning, no coupling, no per-layer widths in the copper, `max_length` still not enforced (all B / C).
- `engine._frugal` / `_key` constraint-miss keys (B3), `diag.critique` kinds, `z_estimate`, `PAIR_SKEW` "which
  bend caused it".
- `via_count` rules are read into `Constraints.max_vias` and KiCad's `too_many_vias` markers are counted, but the
  router does not compare its own via counts against them.
- Not measured in KiCad: `(constraint skew … (within_diff_pairs))` (read as a pair skew), three or more track ends
  on one pad centre, explicit stackups with sub-layers (parsed, heights by the same rule), blind / buried vias,
  arcs (kept arcs arrive as chords and are measured as such; no `approx` flag yet), another KiCad language.

## Electrical constraints, step B (design §14.6 / §14.7: length tuning) — as built

Engineer B of three (2026-11-11). Files: new `v2/tune.py`, `tests/test_v2_tune.py`; edits in `v2/judge.py`
(`tune_pass`, `_tune_blame`), `v2/engine.py` (`_Run.tune`, `TUNE_SHARE`, `_misses`), `v2/classify.py` (group order),
`v2/polish.py` (two skips), `v2/constraints.py` (`measure`: tuning columns), `v2/types.py` (piece stage `"tune"`),
`tools/route_tools.py` (finding text, help text), `tests/test_v2_constraints.py` (the order test follows the new
group order). Results: `bench/route/results/constraints_tune.md` (before / after per group), `cons_B.md`,
`cons_B_off.md`, `cons_gateB.md`. Pairs are still two nets routed one after the other (step C).

**What exists now (C reads this first)**

1. `tune.run(ctx, budget, only=None, cal=None) -> StageReport` — per tuned group (`cons.tuned()`, by name), per
   member (by name): `L = constraints.net_length(net).total + cal.get(net, 0)`; window from `constraints.window()`;
   a member with `L < lo + GUARD` (2 µm) is brought to the *aim*: `longest − min(tol / 4, 0.05)`, or `len` exactly
   with an explicit target, or `lo + min((hi − lo) / 4, 0.05)` for a pure KiCad `length (min)(max)` window. A net
   that already carries this run's meanders is first put back to its routed shape (`untune`) and tuned afresh, so
   calling the pass again is idempotent. `data = {changed: [nets], nets: {net: record}, groups, tried}`; record =
   `{group, before, after, aim, added, bumps, runs, amp?, short_by?, long_by?, capacity?, why?}`.
   `tune.untune(ctx, nets, avoid=None)`, `tune.capacity(ctx, net)`, `tune.gain(a, c)`, `tune.shallow_gain(h)`,
   `tune.params(w, clr, spacing, amp)`, `tune.bump_points()`, `tune.allocate()`, `tune.tuned_conns(ctx)`.
2. Bookkeeping: `ctx.history["tune"] = {edits: {net: [{old: piece fields, new: [piece ids]}]}, avoid: {net:
   [rect]}, banned: {net: why}, nets: {net: record}}`. `constraints.measure` reads `nets` from there: group rows
   gain `tuned` (members with meanders), `added` (mm), and on a miss `why` + `capacity`; the full report gains
   `tuning: {net: [before, after, bumps, why?]}` (dropped by the compact form like `nets`).
3. An edit = ONE straight state segment replaced by a polyline with the same two end points, in one `RouteState`
   transaction, kept only when every routed connection of the net is still `connected` (strict where it was
   strict, loose where it was loose). New pieces: `stage="tune"`, the run's `conn`, `w`, `target_w = w`,
   `protected=False`. `untune` re-adds the old piece with the same fields and a NEW id (the export order of that
   net's tracks changes; the copper does not).
4. Candidate runs: state segments of the net, not `stage == "tune"`, not `protected`, `conn >= 0` and routed, full
   width (`w == target_w`), axial or diagonal (an odd-angle segment is skipped: a bump on it would not be
   octilinear), at least `2 keep + 0.36` long, and **no other copper of the net attached to its interior** (end
   points of other pieces, vias, kept tracks / vias, pad centres: "not teed onto"). Longest 16.
5. Shapes: the U bump of §14.7 (8 points; feet on the run `S + 2c` apart, legs `S` apart) and the shallow bump
   (4 points, flat 0.2 mm). `S`, `c`, `a_min`, keep-off exactly as the table in §14.7; `spacing` and the starting
   `a_max` come from `cons_env["tuning"]["single"]` (`spacing`, `max_amplitude`), else 0.6 / 1.0.
6. Legality of a bump (`_Tuner.ok`): every segment `oracle.seg_legal(layer, …, w, net, clearance)` (other copper,
   holes, keep-out rule areas, edge, plan forbid / avoid / strict corridor), `oracle.soft_hits` ⊆ what the run
   itself crossed, outside the net's `avoid` rectangles, and the **own-net rule the oracle does not have**: every
   segment keeps `S − w` (edge to edge) from the net's own pads and vias, and every segment except the two feet
   keeps it from the net's own tracks (state pieces other than the run, kept tracks). Own zones are ignored.
   The tuner reaches into `oracle._static`, `oracle._dyn` and `oracle._pieces` for that (there is no public
   "shapes of my own net" call; `oracle.near()` does not say which shapes are dynamic).
7. Survey = first fit sliding along the run in 0.1 mm steps, both sides tried at every position, amplitude ladder
   `a_max, ¾, ½, ⅜, ¼, … a_min`; the taller side wins, ties keep the previous bump's side (first: +1 = left of the
   run's direction with y up). Allocation (`allocate`): fewest bumps — tallest slots first — then the amplitudes
   are lowered from the last one backwards until the sum is the need; when even the lowest bumps overshoot, one
   bump fewer and one or two shallow bumps take the rest. **Which run**: the one that covers the whole need with
   the fewest bumps (ties: the longest, then canonical); if none can, the largest capacity first and the next run
   takes what is left. A need below one U bump: shallow bumps on the longest run that has room.

**Deviations from §14.7 / §14.6, each measured or reasoned**

8. **Amplitude escalation (new).** KiCad writes `max_amplitude: 1.0` into every project file, so "the project's
   amplitude" cannot be told from "nobody chose one". When the plan gives no `groups[].amp` and an attempt leaves
   a member short, the net is put back and laid again at 2 × and 4 × that amplitude (at most `AMP_AUTO_MAX = 4`
   mm); the attempt that ends longest stays and the record carries `amp`. With `groups[].amp` the plan's value is
   a hard limit. Measured on `cons_mixed`: `/Q_IO0` reaches its window only this way (see the results file).
9. **Group order (the "bias" asked for).** `classify.GROUP_BIAS = "long+prio"` (COPPER_V2_GROUP_BIAS = `none |
   prio | long | long+prio` for measurements): the members of a tuned group share one order key — where the
   group's shortest member would have come — and sort **longest estimate first** inside it, at priority
   `max(own, 70)`. Reason: the longest member is the group's target; routed last it gets the detour and every
   other member has to match it. Measured on `cons_bus_match` with the final tuner (no plan, KiCad skew rule
   0.5 mm; every variant ends inside the rule with 0 new DRC errors — the difference is what it costs):

   | order | target mm | meander mm | board total mm | vias |
   |---|---|---|---|---|
   | `none` (step A's order) | 36.66 | 43.7 | 204.7 | 0 |
   | `prio` (§14.6 as designed) | 36.66 | 33.2 | 204.0 | 2 |
   | `long` | 36.66 | 43.7 | 204.7 | 0 |
   | **`long+prio`** (default) | **29.74** | **16.1** | **170.2** | **0** |

   (`long` alone changes nothing here: the group's clock, at priority 85, and the unconstrained net still come
   first.) Before the amplitude escalation of item 8 existed, `none` and `long` missed the rule by 0.58 and
   1.38 mm at 1 mm amplitude. `cons_mixed`: the same copper
   under `long` and `long+prio`, the group is not met under any order (item 14). A member that is the N net of a
   pair, or whose own priority is above 70 (a clock at 85), keeps its place by priority.
   Boards without a tuned group: `lead` and `tuned` are empty, every rank is what it was.
10. **`tune_pass` loop** differs from the pseudo-code in three points: (a) the blame is per marker — the tune
    pieces `kio.marker_pieces()` names, else the nearest tune piece within 1 mm, plus tuned nets whose KiCad
    open count rose; (b) a net KiCad rejects in each of the 3 attempts is *banned* (stays as routed, record
    `why: "meander rejected by KiCad at (x, y): <type> (in each of 3 attempts)"`) and one more commit + DRC
    confirms the board without it, instead of throwing every other net's meanders away; (c) the calibration uses
    KiCad's length only for nets KiCad still flags (`chk["cons"]["len"]` exists only for flagged nets). At most
    `TUNE_ROUNDS + 2 = 5` KiCad round trips, 1 on a board where the first attempt is accepted, 0 without a tuned
    group. The pass needs `j["last"]` (a judged board) — a plan-only run (`stages` without `judge`) lays nothing.
11. `engine._Run.tune()` runs after `pours_pass`, before `judge.cleanup`; `TUNE_SHARE = 0.05` of the budget as a
    floor; pass record `{"pass": "tune", connections: tuned + missed nets, routed: tuned}`. It also runs inside
    each via-frugal rung and the second opinion (they are full pipeline runs). `_frugal`'s key is now `(constraint
    misses, vias, length, mult)` and the second opinion wins on `(errors, opens, misses)`; `_key()` itself is
    unchanged (it is compared with `(0, 0)` in three places).
12. `polish._sweep` and `polish.revia` skip a connection that carries a `"tune"` piece. In the pipeline polish
    runs before the tuner, so this matters only for callers that polish afterwards (C: `engine.reroute` after a
    tune would rip the connection and its meanders — nothing does that today because the tune pass is last).
13. Not built from §14.7: the `mode="skew"` entry (C2), the optional reserve of step B5, `diag.critique` kinds, a
    `suggest[]` patch for `LENGTH_MISMATCH` (the fix text names `groups[].amp`, `tol`, `len`).

**Limits found while measuring**

14. `cons_mixed`: the QSPI group is **not met** and is reported as such. Two members route at 21 mm (they cross
    the others), three at 9 mm between neighbours at 0.8 mm pitch: `/Q_IO0` is tuned into the window (taller
    bumps into the free area beside it, item 8), `/Q_IO3` gets 5.5 of the 11.5 mm it needs, `/Q_CLK` (a 5 mm run
    with a track 0.8 mm away on either side, the rest fan-out stubs) has no free run at all. The honest fix is in the report (`why`, `capacity`): room beside those runs, or a looser rule.
15. A member's meanders are laid in net-name order; an earlier member's bumps can take the room a later one
    needs. No fairness pass.
16. A net in a tuned group whose copper was on the board before the run is reported (`why: "its copper was on
    the board before this run …"`), never altered. One net of a `coupled` pair is not tuned net by net either.
17. Tolerances below ~0.09 mm: the smallest shape adds 0.066 mm; a member that needs less than that to enter
    its window and has less than that of headroom stays where it is and is reported short.
18. Every bump is 7 pieces (8 with the stretch of run after it): a group that needs 16 mm adds ~110 track
    segments. `Score.bends` rises accordingly (it is not gated anywhere after the judge).

## Electrical constraints, step C (design §14.8: coupled pairs, width rules, keep-short) — as built

Engineer C of three (2026-11-11). Files: new `v2/pairs.py`, `tests/test_v2_pairs.py`; edits in `v2/detail.py`
(`route_conn(accept=)`, `_route_window(accept=)`, the pair dispatch in `run`, `layer_width`), `v2/oracle.py`
(`set_ally`, `pair_need`), `v2/tune.py` (`skew_net`, `skew_pair`, `skew_height`, `forget`), `v2/judge.py`
(`tune_pass` runs for pairs with a skew limit; `_pair_rules_worse`), `v2/engine.py` (`reroute` takes the partner,
`enforce_max_length`, `_pairs_wanted` / `_pairs_opinion`), `v2/repair.py` (`_net_protected`), `v2/polish.py`
(two skips), `v2/classify.py` (width rules, keep-short order), `v2/constraints.py` (`skew_pairs`, class-width
rules, the pair / width / short rows), `tools/route_tools.py` (findings, compaction, help text). Results:
`bench/route/results/constraints_final.md`, `cons_C.md`, `cons_C_off.md`, `cons_gateC.md`, `v2_cons.md`.

**How a pair is laid (`pairs.route_pair`)**

1. `pairs.jobs(ctx)`: for every `cons.coupled_pairs()` pair, the P connections and N connections whose two ends
   lie within `REACH = 4 mm` of each other end for end (greedy by distance, canonical). Open (pour) ends and
   connections with no partner stay sequential. `detail.run` asks `_pair_jobs(ctx)` once per call and, when it
   reaches EITHER member, calls `route_pair` first (once per job and call) and then `route_conn` as always: after
   a success that returns `how: "already"`, after a refusal it routes the connection on its own. So the plan's
   `diff_pairs` are not needed for the order: a pair detected with no plan is laid when its first member comes up.
2. **Gather** (`pairs.gather`): exit direction `u` (the one straight out of the pad row first, then by how well it
   points at the other end, 4 tried), lane spacing `s = width + gap`. Each track: pad centre → 45-degree diagonal
   → its lane (a straight lead out of the pad first when the diagonal alone is illegal, or when it would put a
   lane exactly ON a pad edge — measured: KiCad then counts the in-pad stretch of one net and not of the other,
   0.06 mm on `cons_mixed`; the lead is skipped when a KiCad `diff_pair_gap (max)` would flag the parallel exit).
   The gather point G is the first point at least `max(0.3, s)` past the diagonals where the rung between the
   lanes keeps 0.05 mm from the pair's pads and a track `2w + gap + 0.05` wide is legal. Both lanes get a vertex
   at G (KiCad counts a lane segment as coupled only when the rungs at its ends are clear).
3. **Centre line**: `detail.route_conn(virtual connection, accept=...)` — the P connection with terminals G_a /
   G_b, width `2w + gap + 0.05`, `neck = width`, via `(pv + via, pv + drill)` with `pv = max(via + via_gap, drill +
   hole_to_hole, s)`, `tree_target=False`. While it runs **the oracle treats the N net as the P net's own**
   (`Oracle.set_ally`; the window LRU is cleared before and after): without that the N pads walled the centre
   line in at every fine-pitch part (measured on `cons_usb_pair`: no gather point at U1). `accept` gets the
   emitted centre specs instead of `state.add`.
4. **`pairs._accept`** (ally switched off): centre specs → runs per layer; `offset(±s/2)` — mitred corners, a
   centre piece too short for its two corners is dropped from the inner lane (which then turns in one corner,
   never nearer than the gap); at a layer change two vias at `±pv/2` across the bisector of the incoming and
   outgoing direction, joined by 45-degree pieces (a turn of more than 90 degrees at the via, or no room for the
   jog, is a `Blocker` back to the search); fan-ins prepended / appended. **Every** segment and via is asked of
   the oracle as its own net — P first, added, then N against the state with P in it — in one transaction that is
   committed only when both connections are `connected`. A refusal returns a `Blocker` on the centre line
   (`win.last_reject` set), `_block_reject` closes those cells and the search runs again (`RESEARCH`).
5. **Escapes**: pads the fan-out stage gave an escape stub are first tried from their centres (`_unescape`: only
   escapes that serve these two connections alone; dropped on success, put back otherwise).
6. **Skew at once** (deviation from §14.6, which tunes everything after the judge): right after the lanes are
   in, `tune.skew_pair` lays shallow bumps (`h <= min(max(3 gap, 0.2), 0.3)`, and below `gap_hi - gap - 0.02`
   when a KiCad rule gives a max gap) on the shorter net, runs nearest its pads first. Measured on
   `cons_usb_pair`: after the other nets are routed the inner lane has a neighbour at the clearance on its outer
   side and not one bump fits (skew 0.70 mm stays); laid at once, 3 bumps bring it to 0.05 mm. The judged pass
   (`judge.tune_pass`, now also run when `cons.skew_pairs()`) only ADDS bumps to what is there, re-tunes against
   KiCad's figure, and undoes bumps for good when KiCad then reports the pair's gap / uncoupled rule
   (`_pair_rules_worse`).
7. **One object afterwards**: `repair._net_protected` is true for nets with coupled lanes; `polish._sweep` /
   `revia` skip `pairs.coupled_conns(ctx)`; `engine.reroute` calls `pairs.with_partners` (a lane the judge ripped
   takes its partner's lane out; `detail.run` then lays both again, or each on its own).
8. `ctx.history["pairs"] = {P net: {gap, conns: {P conn idx: {partner, coupled, why, vias}}}}`. It can be stale
   after a state restore: `coupled_conns` checks the state, and the report's `coupled %` is geometry.

**Budget.** A pair attempt has its own allowance (`detail.PAIR_CAP = 300 000` units per job and `detail.run`
call, not charged to the stage: `data["pairs"]["work"]` says what was used; `COPPER_V2_PAIR_BUDGET=shared`
charges the stage instead). Measured on `cm5_minima`: charging the stage or not gives the same copper.

**Completion first (`engine._pairs_opinion`, not in the design).** When a judged result is not complete and
clean AND pairs that were only *detected* (src `name` / `class`) were laid as lanes, the board is routed once more
with those pairs as two nets, and that run is kept when KiCad judges it strictly better (fewer new copper errors,
then fewer open connections). The pair rows then say `lanes: false`, `why: "completion first: ..."`. Plan- and
rule-backed pairs are never un-coupled this way. Reason, measured: `cm5_minima` ends 198 / 208 with its class
pairs coupled (three of them at 94 – 97 %) and 199 / 208 with them as two nets (the `v2_stage7b` figure); neither
the pair budget nor the protection of the lanes explains the lost connection (`/CM5/PCIE_PI.RX_P`, "time budget
used up before this connection could be repaired") — the board is budget-starved either way and the copper
differs. Cost: such a board is routed twice. `COPPER_V2_PAIRS_ALT=0` switches it off.

**Width rules.** `classify.widths`: a `WidthRule` with `neck=False` (plan `widths` / `z`, a KiCad `track_width
(opt)` rule) sets `width = max(rule width, layer widths)` and `neck = width`; `detail.layer_width` lays each
layer's own width in `emit` and in the pattern branch (`target_w` = that width, so polish does not re-widen).
A pair's centre line and a neck rung are left alone. Impedance-named classes and **every net class whose track
width differs from the Default class's** are `WidthRule(src="class", neck=True)`: the router lays the class
width as it always did (`ConnV2.width`), may neck (the existing rungs), and the report splits what is thinner
into `widths.necked` (both ends within 3 mm of a pad of the net) and `widths.off` rows ending in `"class"`
(information; `WIDTH_NOT_HELD` at info level). Not done: per-layer widths for the two nets of a pair, widths
laid by the v1 fallback search (it lays `ConnV2.width` — shows up in `widths.off`), `z_estimate`.

**Keep-short.** `classify._connections`: nets in `cons.short` that are not already `critical` rank with the
critical ones (third rank element). `engine.enforce_max_length` (after polish, before the judge): only the
plan's `max_length` is enforced by taking a connection out (its field text has always said "reported as failed,
not written"); a KiCad `length (max ..)` rule is reported (`short[].long_by`, KiCad's own error counted) —
deviation from §14.2's "hard for both", because an open connection is worse than a long one unless the plan
asked for it. Soft keep-short nets report `detour` when longer than `1.3 x estimate + 1 mm`.

**Not done / not verified in step C**

- Serpentines of a coupled pair as a pair; a pair that is a member of a tuned length group is not tuned at all.
- Pairs on multi-pad nets couple only where a P connection and an N connection match end for end
  (`phone_breathalyzer`: the USB pair runs through a SOT-23-6 protection part with a pad between the two nets —
  "no legal fan-in at U4", routed as two nets, the copper is what it was).
- `pair_confidence` (step A) gives 0.3 to pairs whose two nets end on different parts (series resistors):
  `stickhub`, `one_air_max` — never attempted as lanes, reported with that reason.
- Via pairs with a turn at the via, blind / buried vias, arcs, teardrops at the fan-in: not built / not measured.
- KiCad's coupling verdict was measured on the four bench boards (no pair error on any of them);
  `within_diff_pairs` skew rules, pairs on inner layers next to refilled pours: not measured.
- `diag.critique` kinds for constraints and `out["diff_pairs"]` retirement (§14.12 C4): not done; `PAIR_SKEW`
  from `out["diff_pairs"]` is still suppressed for pairs with a limit (step A).

## Electrical constraints, fix round 1 (after the independent verification of step C) — as built

Fix engineer, 2026-11-12. An independent verifier routed the four constraint boards through the tool layer and
found six things wrong; this section says what each was at its root and what is now built. Files: edits in
`v2/tune.py` (hops, skew ladder and aim, whole-width forbid test, nested edits), `v2/constraints.py`
(`lay_width` / `layered`, `coupled_stats` tolerance, `lanes` for copper from before the run, `hops` in the group
row), `v2/detail.py` (`layer_width` delegates), `v2/fanout.py` (`_layer_width`), `v2/legacy.py` (per-layer
width in `_commit`), `v2/judge.py` (`tune_pass`: hops go first when nobody can be blamed; the note),
`guided/session.py` (`_pair_report(cons=)`), `tools/route_tools.py` (help text, one finding text, the note that
survives compaction). Tests: new `tests/test_v2_consboards.py` (the bench boards end to end, KiCad's DRC and
KiCad's lengths); additions in `tests/test_v2_tune.py`, `test_v2_pairs.py`, `test_v2_constraints.py`. No
existing test was edited. Results: `bench/route/results/constraints_fix1.md` (and `cons_F1`, `cons_F1_off`,
`cons_gateF1`, `v2_fix1`).

**1. `cons_mixed`: members boxed in by their neighbours — "hops" (`tune._hop`, `_hop_line`, `_hop_loop`).**
Root cause: a meander could only be a bump beside an existing straight run on the run's own layer. `/Q_CLK` is a
5 mm run with a track 0.8 mm away on either side, `/Q_IO3` has `/Q_CLK` above and a 0.5 mm supply track below:
`capacity 0.0`, on a board whose B.Cu is empty. What is built:

1. `tune_net` tries every amplitude on the net's own layers first (as before: base, 2 x, 4 x). Only when none of
   them closes the gap, the same amplitudes again **with a stretch of the net on another layer**: `_tune_at(...,
   hop=True)` lays the hop first, while every run is still straight, then the ordinary bumps on what is left
   (the two parts of the hopped run before and behind its vias stay candidate runs: `history["tune"]["free"]`).
   At most `HOP_MAX = 2` hops per net. The attempt that ends longest is kept, as before.
2. A hop = two through vias on ONE straight run of the net (`_via_sites`: every 0.25 mm) and between them, on a
   layer the net may use (`layers_ok`; farthest layer first), either
   * the run itself with U bumps / shallow bumps on it (`_hop_line`: the shortest stretch that holds what is
     missing; bumps keep `S - w` from the stretch's own vias), or
   * where the run is too short for that or its line is taken on the other layer, **one loop** (`_hop_loop`):
     the vias `d = max(S, via + clearance, drill + hole_to_hole)` apart (rounded up to the site step: 1.0 mm
     with 0.6 mm vias), the legs straight out of the via centres at right angles to the run for the amplitude
     `A`, a chamfered top. Gain `2A - 2c(2 - sqrt 2)`, exact because nothing turns inside a via pad.
3. The two vias add `2 x stack height` to the net (KiCad counts it; `Stackup.height`, measured in step A). A hop
   whose vias alone would overshoot is not laid (`why: "... would add 3.16 mm, more than is missing"`): on a
   2-layer 1.6 mm board a member short by less than 3.16 mm that has no room on its own layer stays short.
4. Via sites: `oracle.via_legal` on every layer, no reserved (soft) site, the via's whole pad outside
   `regions[].forbid` / `nets[..].avoid` / `regions[].no_vias` (the oracle tests a via's centre only), the
   clearance kept from the net's OWN pads (no via in or against a pad), outside the rectangles KiCad rejected a
   meander of this net in. `via_room`: the plan's `nets[..].max_vias` and a KiCad `via_count` rule; `max_vias: 0`
   keeps a net on its layer.
5. Bookkeeping: an edit record gains `old_id`; `untune` maps a piece that an undone later edit put back to its
   new id, so a hop and the bumps laid afterwards on its left-over parts are undone together. `record["hops"]`,
   `record["hop_layers"]`; `constraints.groups[].hops` = members with a hop; a note names them ("... carry a
   stretch on another layer through two vias each: /Q_CLK (B.Cu), ..."), and `route_tools._compact_v2` keeps
   that note in the default result.
6. `judge.tune_pass`: when KiCad reports a new error or an open connection that no meander can be blamed for
   and some nets carry a hop, the hops go first, for good (`history["tune"]["nohop"]`), and those nets are
   tuned again on their own layers; only then is the accepted board put back. (A stretch on another layer is
   the one thing that can cut a pour THERE off; the other nets' meanders must not pay for it.)
7. Not done: choosing the hop layer by what is poured there (a hop may land on a layer with a ground pour; item
   6 is the safety net, not a preference), blind / buried vias, a hop for the nets of a coupled pair, hops for
   intra-pair skew, fairness between members (name order still decides who gets the room first).

**2. An unreachable skew limit made the copper worse.** Root cause: `skew_pair` aimed at `max(lo + 2 GUARD, hi -
skew / 2)`; with `skew = 0.005` that is 1 um below the longer net — inside the tuner's own 2 um guard — so the
three bumps laid right after the pair "would have made the net the longer one of its pair" and were ALL undone;
the judged pass later found room for one. Now: `aim = hi - max(1.5 GUARD, min(skew / 2, 0.05))` (never inside
the guard), the acceptance test is `lo + min(GUARD, skew / 4)`, and `skew_net` tries lower bumps
(`SKEW_LADDER`: the highest, 0.6 of it, the lowest there is) where the high one does not fit instead of giving
up. A limit below ~0.004 mm (possible in a KiCad rule; the plan's range starts at 0.005) ends 0.003 mm from the
longer net and is reported as missed by the difference — with the bumps kept.

**3. Per-layer widths on the unlisted layer.** Root cause: `classify.widths` sets `ConnV2.width` to the widest
layer (the search runs at it) and only `detail.emit` asked `layer_width`; the fan-out stage laid its pad escapes
at `ConnV2.width` (0.3 mm on F.Cu where the net's own 0.2 mm applies), and the v1 fallback search laid
`cfg.net().width` with `target_w = ConnV2.width`, which polish then widened to 0.3 on a 0.25 mm layer. Now
`Constraints.lay_width(net, layer, full)` is the one answer (`layered(net)` says whether a rule applies):
`detail.layer_width`, `fanout._layer_width` (both job builders) and `legacy._commit` ask it.

**4. `out["diff_pairs"]` contradicted `constraints.pairs`.** `session._pair_report` takes the constraints block
of the same result: a pair with `lanes: true` reads `"coupled lanes: 84 % of the pair runs side by side at 0.15
mm (figures: constraints.pairs)"`, and `len_p / len_n / skew_mm` are the block's figures (via height included)
for every pair that has a row. A pair routed as two nets keeps the old text. (`diff_pairs` is still there:
`tests/test_guided_route.py` reads it.)

**5. The pair figures changed when the same copper was measured again.** Two causes. (a) `coupled_stats` called
two segments parallel only below `sin = 1e-6`; the board extract rounds coordinates to 0.1 um, which turns a
0.4 mm piece of a skew bump by 2e-4 rad, so 0.4 mm of lane dropped out of the coupled length on every call that
read the copper back from the file. Now: parallel when the directions drift apart by less than `PAR_DRIFT = 0.5
um` over the shorter segment (never above `sin = 0.02`). (b) `lanes` came from the run's own record
(`history["pairs"]`), which a call that routes nothing does not have. For a pair none of whose connections is
in this run, `lanes` is what the copper is: side by side at its gap for at least `LANES_PCT = 50 %` of the
longer net; otherwise `lanes: false` with `why: "copper from before this run: ..."`. `bench/route/cons.py`'s own
`coupled_pct` still uses the exact test on extract-rounded copper (it reads 82.1 % where the copper is 83.8 %):
the harness was left as it is, its threshold is met either way.

**6. An explicit group length a little above the natural one.** Same root cause and same fix as 1: the inner
members of the bus could add 12 .. 17 mm between their neighbours and needed 17 .. 20. `/SD_D1` and `/SD_D3`
now take a hop to B.Cu.

**Also changed while there: forbid areas bind the tuner's copper with its whole width.** The oracle keeps a
track's centre line out of `regions[].forbid` / `nets[..].avoid`. `_Tuner.ok` now asks both EDGES of every bump
segment as well (`_plan_clear`), and a hop via's whole pad stays out. Nothing else in the router changed in this
respect (the verifier's note stands for ordinary nets: their copper edge may overlap a forbid rectangle by up to
half a track width).

**Measured.** KiCad's own lengths and DRC, before / after per failure, the constraint suite, the gate boards and
the corpus: `bench/route/results/constraints_fix1.md` (the numbers are not repeated here so that they cannot go
stale).

## Electrical constraints, fix round 2 (after the second independent verification) — as built

Fix engineer, 2026-11-12. The second verifier found the constraint features clean and two failures outside
them. Both have ONE root cause, in code that is older than the constraints work. Files: `guided/model.py`
(`canonical`), `guided/session.py` (`_route_v2`, `_constraints_as_is`), `v2/constraints.py` (`measure_board`,
`measure(unconnected=)`), `v2/engine.py` (`_failed`, `_unexplained_open`), `tools/route_tools.py` (help text
only). Tests: additions in `tests/test_guided_route.py` (2), `tests/test_v2_engine.py` (1),
`tests/test_v2_consboards.py` (2); no existing test line was changed. Measured:
`bench/route/results/constraints_fix2.md`.

**Root cause: `model.canonical` wrote into the extract it was given.** `Board(ex)` orders everything by position
and renumbers KiCad's connectivity clusters by pad order (so the same design routes the same way). It sorted
the pad dicts of the caller's extract and overwrote their `c` with the new number; tracks and vias were copied
and renumbered through the same raw -> new table. The first `Board(ex)` was right. Every later Board built from
the SAME extract — `session.make`, `engine.Engine` (`self.ex`), the judge's Boards of one check's extract — then
looked the tracks' RAW ids up in a table whose keys were already the NEW pad numbers:

* raw id not among the new numbers: the track / via got cluster 0 — existing copper belonged to no pad;
* raw id equal to another cluster's new number: the copper joined THAT pad's cluster.

Which of the two happens depends on KiCad's raw numbering, i.e. on the order of the footprints in the file, and
KiCad saves footprints in UUID order: the same board built twice comes in two orders.

1. *The intermittent gate failure* (`test_e2e_four_layers_locked_track_and_open_board_guard`, 3 of 27 runs).
   The locked `/D0` track starts on J1.3. In the failing order its raw cluster id was 26 and U1.1's NEW number
   was 26: for the engine the track was U1.1's copper. It "connected" J1.3 to the track with a 1 mm loop, the
   U1.1 escape stayed where it was, KiCad counted one open connection, and `failed` was empty because the model
   had the connection routed. In the passing order the id was 30: cluster 0, and the pads were joined directly.
2. *The lost constraints block on `cons_mixed`.* On the routed board the GND vias / tracks got wrong clusters
   in the engine's Board, a pad that hangs on the In1.Cu plane counted as cut off, the power stage proposed a
   dog-bone, the judged run closed nothing, was discarded, and the session dropped `constraints`.

Fix: `canonical` copies the pad dicts; the extract is never altered (`tests/test_guided_route.py::
test_one_extract_builds_the_same_board_every_time` — the tests had been working round it with
`json.loads(json.dumps(ex))`). Effect on a board without existing copper: none (no track or via to number; the
pad numbers of a second pass were the identity already), which is why the corpus signatures do not move.
`test_e2e_existing_copper_routes_the_same_whatever_the_footprint_order` re-orders the footprints of the test
board into the verifier's failing order, a passing one and a third, and asserts 24/24, KiCad DRC clean and the
SAME copper for all three.

**Also built, so that the next disagreement of this kind cannot hide:**

* `engine._failed`: a net KiCad's ratsnest of the judged board shows open, that is a target, has no pour and no
  record of its own, gets one: `reason: "KiCad still shows N open connection(s) on this net although the router
  had every connection of it laid ..."`, `from` / `to` = one pad of each of the first two clusters KiCad holds
  apart (the judged extract; the net's first connection without one). With the old `canonical` the verifier's
  run now reads `failed: [{net: "/D0", from: "J1.3", to: "U1.1", ...}]` instead of `[]`.
* `session._route_v2`: a run that keeps none of its copper (judged but nothing closed; a rip-up undone) is
  `reverted`. Its `constraints` block described the discarded copper and was dropped — while `diff_pairs` had
  already been written from it ("figures: constraints.pairs"). Now `_constraints_as_is` replaces the block by
  `constraints.measure_board(board of the file, cfg.x, KiCad's open connections of the file)`: existing copper
  only, no `kicad` key (no DRC was asked about that copper, so a `.kicad_dru` group is judged by the router's
  own figures), no `tuned` / `hops` / `tuning`; `diff_pairs` is written from that block. It is the same
  measurement a "nothing to route" call makes.
* `not_written` for a reverted run with `total == 0`: "every connection the plan selects is already made, and
  the copper proposed on top of it connects nothing: nothing was written, the board is unchanged" (was: "no
  connection could be routed with this plan ... (see failed / findings)" with both lists empty).

**Not done.** The verifier's other observations are unchanged: an unreachable `groups[].len` keeps its partial
meanders (reported, but the copper is worse than untouched against the board's own rule); members routed in an
earlier call are measured, never altered; `track_dangling` warnings from unused fan-out stubs on unconstrained
nets; name detection misses `CLKP/CLKN`, `CK_T/CK_C`, `X/X#`; ordinary nets' copper edges may overlap a forbid
rectangle by up to half a track width; `cm5_minima` is routed twice (wall time). The `Board`s the judge builds
of one check's extract were affected by the same defect; no corpus board changed with the fix (signatures in
`constraints_fix2.md`), so no judge decision on the corpus had depended on it.
