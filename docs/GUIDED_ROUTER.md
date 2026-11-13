# Guided autorouter — `pcb_route_plan` + `pcb_route_guided`

**The model writes the heuristics, a deterministic algorithm routes, KiCad judges.**

Copper never calls a language model. Whatever model drives Copper (any vendor, any size, or none)
hands the router a *plan* — plain JSON: which nets go first, how wide, on which layers, along
which corridor, which areas to stay out of. A pure-Python rip-up-and-reroute engine turns the plan
into tracks and vias. Then KiCad's own DRC and ratsnest decide what counts as routed. With no plan
at all the built-in heuristics are used, so the router also works with no AI.

* Same plan + same board ⇒ the same copper, every time (no randomness, stable ordering, no dependence on file order).
* A connection is only reported as routed when KiCad's connectivity confirms it; new copper that
  takes part in a KiCad DRC error is removed again and reported as unrouted.
* All-or-nothing: plan errors are rejected before anything is touched, the board is checkpointed
  before the write and restored byte-for-byte if anything fails.

It lives next to `pcb_route(mode='auto')` (Freerouting). Use `compare=true` to score one against the other.

## The loop

```
pcb_route_plan()                      → facts: nets, congestion, layers, keep-outs, suggested_plan, schema
   edit suggested_plan
pcb_route_guided(plan=…, dry_run=true)→ real numbers + picture, board untouched      (optional)
pcb_route_guided(plan=…)              → routes, refills zones, DRC, findings
   read failed[] → adjust the plan → pcb_route_guided(plan=…, ripup='all' | ripup='nets', nets=[…])
```

How to read a failure and what to change:

| `failed[].reason` | what it means | plan change |
|---|---|---|
| `blocked by routed nets: A, B` | A and B were routed earlier and sit in the only way through | put the failed net first: `order: ["<net>", "*"]`; or give A a detour: `nets.A.avoid` / `corridor`; or lower `global.via_cost`; or raise `global.ripup_rounds` |
| `pad X at (x, y) is outside the board outline` | the part is not placed on the board (finding `PARTS_OFF_BOARD`) | `pcb_place` it, then route again |
| `pad X is boxed in …` | no room for a track of this width + clearance next to the pad | smaller `width`, more `layers`, or move parts (`pcb_place`) |
| `pad X lies inside a forbidden region …` | your own `regions[].forbid` / `avoid` / strict corridor traps the pad | add the net to `regions[].except`, or use `cost` instead of `forbid` |
| `no path inside the strict corridor` | the corridor does not connect the pads | widen it, move the points, or `corridor_strict: false` |
| `needs N vias but max_vias=…` / `… over max_length=…` | the constraint cannot be met with this placement | relax it or move the parts closer |
| `search effort limit …` / `time budget used up …` | big board, not enough time | raise `global.time_budget_s` / `effort`, or a coarser `global.grid` |
| `to: "pour"` … `walled in` | a pad of a poured net (GND…) cannot reach its pour or drop a via | the router already retried with the neighbours moved away; free space near `where` or add a via by hand |

`where` is always a board coordinate in mm; `blocked_by` names the nets in the way.

## Plan schema

Every field is optional. Lengths are millimetres, coordinates are KiCad board coordinates (x right,
y down — the same numbers `pcb_read` and `pcb_route_plan` report). Net names may be exact
(`"/USB_DP"`), the short form of a sheet-local net (`"USB_DP"`) or globs (`"/IO*"`, `"*"`). In
`nets`, exact names win over globs and later globs win over earlier ones. Unknown fields, nets or
layers raise `BAD_PLAN` with the valid choices; nothing is written.

```jsonc
{
  "order": ["/XTAL*", "/USB_D?", "*"],     // routing order; unlisted nets: by priority, then short first

  "global": {
    "grid": 0.1,                           // mm, or "auto" (0.1 on small boards, coarser on big ones)
    "via_cost": 5,                         // one via is worth this many mm of track (lower = more layer changes)
    "bend_cost": 0.3,                      // one 45° bend is worth this many mm (higher = straighter)
    "style": "45",                         // "45" or "90" (orthogonal only)
    "layer_directions": {"F.Cu": "h", "B.Cu": "v"},   // h | v | any
    "direction_bias": 0.5,                 // 0..3 cost factor for running against a layer's direction
    "layer_cost": {"B.Cu": 1.2},           // >= 1: make a layer less attractive (keep a plane intact)
    "layers": ["F.Cu", "B.Cu"],            // layers signals may use (default: all but inner plane layers)
    "ripup_rounds": 3,                     // how often one connection may rip up others
    "time_budget_s": 90,                   // budget in "seconds of a typical desktop CPU", counted as search work
                                           // (not wall clock): what is left when it runs out is reported as failed
    "effort": 1                            // multiplier on the per-connection search limit
  },

  "nets": {
    "<name | glob>": {
      "priority": 80,                      // 0..100, higher routes earlier (ignored for nets matched by `order`)
      "width": 0.4, "clearance": 0.2,      // default: the net class; clearance is never lowered below it
      "layers": ["F.Cu"],                  // the only layers this net may use
      "prefer": ["F.Cu"],                  // other layers cost 2.5x
      "layer_cost": {"B.Cu": 3},
      "direction": {"F.Cu": "h"},          // per-net override of layer_directions
      "via_cost": 20, "max_vias": 1,       // max_vias is per connection; 0 = stay on one layer
      "max_length": 25,                    // whole net, mm — a longer result is reported, not written
      "keep_short": true,                  // route early, ignore direction bias (crystals, RF, switch nodes)
      "avoid": [[x0, y0, x1, y1]],         // rectangles this net must not enter
      "corridor": [[x, y], [x, y]],        // guide path between its pads
      "corridor_width": 3, "corridor_strict": false,   // not strict: leaving costs 3x
      "neck": true,                        // a wide track may fall back to the default width where it does not fit
      "skip": true                         // do not route (e.g. leave GND for the pour)
    }
  },

  "diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "gap": 0.2, "width": 0.2}],

  "regions": [
    {"rect": [x0, y0, x1, y1], "cost": 4, "name": "near the switcher"},
    {"rect": [x0, y0, x1, y1], "forbid": true, "layers": ["F.Cu"], "nets": ["/IO*"], "except": ["GND"]}
  ]
}
```

### What the built-in heuristics do (the `suggested_plan`)

* classify nets from names, net classes and pin types: `ground`, `power`, `diff`, `clock`, `hs`, `analog`, `signal`;
* order: differential pairs, clocks and high-speed nets first, then signals, power, ground — short connections before long ones;
* crystals: `keep_short` + `max_vias: 0`;
* name-matched pairs (`_P/_N`, `DP/DN`, `D+/D-`, `CANH/CANL` …) become `diff_pairs`;
* a ground net with no pour and ≥ 6 pads is skipped (`LEFT_FOR_POUR` finding: `pcb_pour`, then run again);
* a layer that is ≥ 40 % one pour is a plane: inner planes are excluded from `global.layers`, and on a
  2-layer board the quieter outer plane gets `layer_cost` 1.2.

Whatever you put in the plan overrides the heuristic for that net / field only.

## Worked examples

### 1. USB first, classic two-layer discipline

The data pair must be short and on top; everything else follows horizontal-top / vertical-bottom.

```json
pcb_route_guided(ripup="all", plan={
  "order": ["/USB_D?", "/XTAL*", "*"],
  "global": {"layer_directions": {"F.Cu": "h", "B.Cu": "v"}, "via_cost": 3},
  "nets": {"/USB_D?": {"layers": ["F.Cu"], "max_vias": 0, "keep_short": true}},
  "diff_pairs": [{"p": "/USB_DP", "n": "/USB_DN", "gap": 0.2}]
})
```

With the default engine (v2) the pair is laid as two coupled lanes at `width + gap` from pad breakout to
pad entry (a layer change is two vias side by side) and the result's `constraints.pairs[]` row reports both
lengths, the skew, the coupled share and — when the pair could not be coupled and was routed as two nets —
`lanes: false` with the reason (`copper_help(tool="pcb_route_guided")`, "Differential pairs"). The gap is
never below what KiCad allows between the two nets: `/USB_DP` + `/USB_DN` is a pair by KiCad's name rule,
`/USB_DP` + `/USB_DM` is not and gets the ordinary clearance. Impedance is a label (`z`): no width is
computed from the stackup. With `global.engine: "guided"` (the classic router) the pair is routed
*back-to-back* (N directly after P, spaced by the clearance rule) and `diff_pairs` in the result reports
both lengths and the skew.

### 2. Keep signals out from under the antenna and away from the switcher

`pcb_route_plan` lists antenna keep-outs (`copper-antenna-*` rule areas — the router obeys those
anyway). To also keep a margin around them and to make the area next to an inductor unattractive:

```json
pcb_route_guided(ripup="all", plan={
  "regions": [
    {"rect": [114, 123, 131, 132], "forbid": true, "except": ["GND"], "name": "antenna margin"},
    {"rect": [124, 105, 131, 113], "cost": 6, "nets": ["/IO*", "/ADC*"], "name": "switch node"}
  ],
  "nets": {"/SW": {"keep_short": true, "width": 0.6, "layers": ["F.Cu"]}}
})
```

`forbid` is hard (a net with a pad inside can never leave — the result warns about that up front
and tells you to use `except`); `cost` is soft: tracks cross only when the detour would cost more.

### 3. Fix one stubborn net without touching the rest

The first run reported
`{"net": "/IO4", "from": "J2.6", "to": "U2.18", "reason": "blocked by routed nets: /IO5, /TX", "where": [124.1, 111.3]}`.
Re-route just the three nets, `/IO4` first, and steer `/TX` round the other side of the regulator:

```json
pcb_route_guided(ripup="nets", nets=["/IO4", "/IO5", "/TX"], plan={
  "order": ["/IO4", "/IO5", "/TX"],
  "nets": {"/TX": {"corridor": [[116, 104], [116, 112], [121, 114]], "corridor_width": 2.5}}
})
```

Every other track stays exactly as it is (existing and locked tracks are obstacles and connection
points). `previous.restore` in the result is the checkpoint call that undoes the run.

## Engines

`pcb_route_guided` has two engines behind the same plan and the same result:

* **`v2`** (the default) — staged: classify, power reservations, fine-pitch fan-out, global corridors, detail
  search, repair, polish, then KiCad as the judge on a scratch copy; the board file is replaced once, by the board
  KiCad judged. It adds `cuts`, `critique` and `suggest` (ready plan patches) to the result; `detail=true` adds
  the stage reports. Design: `ROUTER_V2_DESIGN.md`.
* **`guided`** (`plan.global.engine: "guided"`) — the classic single-pass rip-up router described under
  *How it works* below. It validates the v2-only plan fields and ignores them (one warning names them).

The rules below hold under both, and `tests/test_guided_route.py`
(`test_e2e_safety_contracts_hold_under_both_engines`) runs them against each engine.

## Safety rules of the tool

* `regions[].forbid`, `nets[..].avoid` and a strict corridor are never routed through. A pad inside a forbidden
  region cannot be connected; its connections are listed in `failed` with that reason.
* `dry_run=true`, `compare=true` and a plan that is rejected (`BAD_PLAN`) never write the board file.
* A run that closes no connection writes nothing — no stubs, no vias, no checkpoint to restore: the file is
  byte-identical and `ripped_up` is 0.
* A ground net with 6 or more pads and no pour is left for `pcb_pour` (listed in `skipped` with its connection
  count, finding `LEFT_FOR_POUR`); `plan.nets["GND"].skip = false` or naming it in `nets=[…]` routes it as tracks.
* `ripup='all'` cannot be combined with `nets=[…]` (it would delete every track and re-route only a few) —
  use `ripup='nets'`. `compare=true` always scores the whole board, so it takes no `nets` either.
* A rip-up after which **no** connection could be routed (a plan that forbids everything, a wrong layer
  set …) is undone: the board file is byte-identical, the result says `not_written` and lists `failed`.
  With engine `v2` the `constraints` block of such a result (and of any run that keeps none of its copper) is
  that of the unchanged board, measured on the file — no `kicad` verdict, no tuning record.
* A rip-up that ends with more unrouted connections than before is written (you asked for it) but flagged
  `WORSE_THAN_BEFORE` with the `checkpoint(action='restore', …)` call that brings the old routing back.
  Try a new plan with `dry_run=true` or `compare=true` first.
* Teardrops are zones glued to the tracks they were made for: a rip-up removes the teardrops of every net
  that loses all its tracks (re-create them in KiCad with *Edit ▸ Edit Teardrops* when the routing is final).
* `drc.errors` is everything KiCad reports on the board, judged with the zones refilled by KiCad itself.
  When errors remain, `drc.new_errors` says how many of them this run added (`0` = all were on the board
  before: placement, footprints, outline) and `drc.new_top` lists the new ones with coordinates.

## Determinism

Same plan + same board = same copper, on any machine: there is no randomness, every ordering is
canonical, and the time budget is spent in *search work* (A\* expansions; `time_budget_s` × 200 000
units), not in wall-clock seconds. A wall-clock safety net exists far beyond the budget (5 × + 60 s); if
it ever stops a run the result carries a note saying that this result may not be reproducible.

## What the router respects

| | |
|---|---|
| Board outline + edge clearance | cells outside the outline, in cut-outs or closer than the edge clearance (+ half the track) are blocked |
| Holes | NPTH and plated holes keep the hole clearance; vias keep hole-to-hole |
| Rule areas | no-track / no-via keep-outs, including footprint keep-outs and `copper-antenna-*` |
| Solder-mask openings drawn as graphics | kept free of new tracks (KiCad's `solder_mask_bridge`) |
| Net classes | width, clearance, via size from the project; board minimums and simple `.kicad_dru` minimums on top |
| Pad layers | SMD pads on their side; through-hole pads can be entered on any allowed layer and act as a free layer change |
| Existing tracks | kept unless `ripup`; new routes may tee onto them; **locked** tracks survive `ripup='all'` |
| Pours | a net with a pour is not routed as tracks: after the zones are refilled, only pads the pour misses get a stub / via, and other nets are moved away when they wall such a pad in or starve its thermal relief |
| Fine-pitch pads | off-grid pads get an exact escape stub checked against real geometry, so the grid does not limit pitch |

## How it works (short)

1. **Extract** (pcbnew worker, read-only): pads as exact shapes, tracks, vias, holes, zones and their
   fill islands, the outline polygon, net classes, and KiCad's connectivity clusters.
2. **Occupancy maps**: for each (track width, clearance, via size) class, a per-layer map says for
   every grid cell whether a track centre may be there — foreign copper is grown by
   `clearance + half width` using exact distances, so tracks on the grid are clearance-correct by construction.
3. **Route**: connections come from a per-net minimum spanning tree over the pad clusters, in plan
   order. Each is an 8-direction, multi-layer A\* (bend, via, layer, direction, region and corridor
   costs). A blocked connection plans a path *through* other routed nets, rips those up, raises the
   history cost where the fight was, and re-queues them (negotiated rip-up).
4. **Clean-up**: cell paths become 45° polylines, ends are snapped to pad centres when an exact
   geometric check allows it, and every segment is re-verified against real shapes before writing.
5. **Write + judge**: tracks and vias are added in one worker transaction, zones refilled, then KiCad
   DRC + schematic parity run. Offending new copper is removed (and reported); pads cut off from
   their pour trigger a re-route with that pad protected.

## Limits (honest)

* Pure Python: about 3–8 connections per second. The ESP32-C3 reference board (38 connections,
  2 layers) takes ≈ 10 s end to end; a 470-connection 4-layer board ≈ 2 minutes (91 %); boards with
  several hundred connections on a fine grid run out of the default budget — raise
  `global.time_budget_s` (max 600) or route in batches with `nets=[…]`. Unfinished work is reported as
  failed, never hidden.
* BGA / HDI boards that were designed around blind or micro vias (8-layer, 0.4 mm pitch) are out of
  reach: expect 50–60 %.
* Engine v2 routes differential pairs as coupled lanes, compensates their skew and tunes length-matched
  groups (see `copper_help(tool="pcb_route_guided")`); what it cannot meet is reported with the amount.
  A group member with no room for meanders beside it gets a stretch on another layer through two vias
  (`constraints.groups[].hops`; `nets[..].max_vias: 0` keeps a net on its layer).
  Not done: serpentines of a coupled pair as a pair, impedance computed from the stackup, pairs on nets
  with a tee. The classic engine (`global.engine: "guided"`) routes pairs sequentially and tunes nothing.
* Through vias only (no blind / buried / micro vias). No arcs, no teardrops.
* `.kicad_dru` rules are read conservatively (largest `min` of each constraint, conditions ignored);
  rules that *relax* a constraint for some objects are not used.
* Very dense boards routed by hand at minimum rules will not reach 100 % — expect ~90–95 % and use
  `failed[]` to finish them, or `pcb_route(mode='auto')` / `pcb_route(tracks=[…])` for the leftovers.
