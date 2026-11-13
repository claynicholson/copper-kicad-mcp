# Copper router v2 — final design

Status: design; stage 0 (W0: shared types, oracle, differential test, Appendix A1/A2) is implemented and measured. Basis: the "pragmatic-evolution" proposal (best on completion/correctness and
buildability), restructured as a staged pipeline and extended with grafts from the other three proposals.

What this document rests on, honestly: the benchmark diagnosis in `bench/route/` (baseline 1788/2186 = 81.8 %, 6/14
boards complete and clean, 5 new DRC errors), the four proposals, the three judge reports, and a reading of the
**signatures only** (not bodies) of `pcb/guided/{router,astar,grid,geom,model,plan,session}.py` and
`bench/route/routers/guided.py`. No code, test or benchmark was run for this document. Every completion or speed
number below that is not the baseline is a **target or projection**, not a measurement.

---

## Owner requirement added 2026-11-11: electrical constraints (binding on every stage)

The product owner requires that the router respects the constraints an engineer would: "certain traces need to be
length matched, etc." This is in scope for v2 and is built as a dedicated **constraints stage (S6c, after polish,
before judge)** once the core stages are stable. Until that stage exists, earlier stages must not make it harder:

- Keep every routed connection's exact length and via count available per net (already in `Piece.length`), and keep
  net-group information (`ConnV2.role`, buses, pairs) intact through repair and polish. Polish must never shorten a
  net that carries a length target below its target.
- Leave the plan fields reserved: `groups[{name, nets[], match: "length"|"delay", tolerance_mm, target_mm?}]`,
  per-net `min_length`/`max_length`/`target_length`, `pairs[{p, n, gap, max_skew_mm, impedance_ohm?}]`,
  `keep_short`, and per-class `impedance_ohm` (maps to a width per layer from the stackup).
- Constraint classes to cover: length-matched groups (memory and parallel buses, clocks fanned out to several
  loads), differential pairs (coupled routing at constant gap, intra-pair skew), controlled-impedance widths,
  keep-short nets (crystals, switch nodes, decoupling), maximum via count, and return-path continuity (no routing a
  constrained net across a split in its reference pour).
- Auto-detection (reported in `auto{}` and vetoable by the plan): `_P/_N`, `+/-`, `DP/DM` pairs; `D[0-9]+`,
  `A[0-9]+`, `DQ`, `CLK` style buses; USB, Ethernet, LVDS, MIPI, HDMI, DDR name patterns; KiCad net-class and
  custom-rule (`.kicad_dru`) length and skew constraints, which are authoritative when present.
- Tuning: serpentine/accordion meanders inserted on the longest free straight runs, amplitude and spacing from the
  clearance rules, every meander checked by the oracle; trombone on diff pairs keeps the pair coupled. If a target
  cannot be met the net is reported with the shortfall in mm, never silently left.
- Benchmark: add boards with real length/skew rules to `bench/route/corpus` and report per group: spread in mm
  against tolerance, pair skew, pair gap violations.

**The design for this requirement is §14 ("Electrical constraints stage").** Where §14 differs from the notes
above it is the contract: tuning runs as a judged pass *after* the judge and the pours pass (not as "S6c before
judge": §14.6 says why), and the plan fields are the shorter ones of §14.3 (`groups[{nets, tol, len}]`,
`diff_pairs[] + skew / uncoupled / coupled / z`, `nets[..].widths / z`, `global.constraints`).

## 0. Decisions in one page

| Question | Decision | Source |
|---|---|---|
| Rewrite or evolve? | Evolve. v2 is a new package `pcb/v2/` that **imports** v1's `grid`, `geom`, `model`, `plan`, worker protocol and work-unit budget; v1 files stay byte-stable except two dispatch hooks. | pragmatic |
| Legality | One exact oracle (`oracle.py`) is the only legality test for verify, fan-out, patterns, polish and repair. Grids are a search accelerator, never the judge. Validated by a differential test against kicad-cli. | human-emulation |
| Rip granularity | Connection (`Piece` sets in a journal). A net is never ripped wholesale; verify and DRC repair remove at most the blamed connection. | all |
| Monotonicity | Keep-best gate on a lexicographic score after every stage and every judge round. More budget can never return a worse board. | pragmatic |
| Search space | Coarse tile graph (negotiated, to convergence) gives each connection a corridor; detail search runs in a short-lived **window grid** whose pitch comes from local pad pitch. No board-wide fine grid. | hierarchical + human |
| Resources first | Pour dog-bone via sites, thermal-spoke capacity and fine-pitch escapes are reserved before signals; reservations are rippable at a high, defined price. | human + topology |
| Vias | Planned globally by a per-connection layer-assignment DP, then removed opportunistically in polish. | hierarchical |
| Stuck | Bounded ladder ending in a certificate that names the cut (pad refs, demand vs capacity, nets). | topology |
| Explicitly **not** built | Delaunay/rubber-band embedding, PNS multi-level shove, CBS/ECBS, BGA Yan–Wong flow, routing through partly poured inner layers by default. | judges |

Why not the higher-ceiling designs: topology-first has no DRC number until its 4th build stage and depends on a
stdlib incremental CDT; hierarchical ends as a second full router with shove and CBS. Both were scored lowest on
delivery by two of three judges. Their best separable ideas are grafted here.

---

## 1. Honest limits and acceptance targets

Multi-layer routing with vias is NP-hard, and some corpus boards may be unroutable at their placement without a
human-grade plan. v2 therefore guarantees **properties**, and has **targets** for outcomes.

Guaranteed by construction (each has a unit test):

1. **Determinism**: same board + same plan ⇒ identical copper (byte-identical commit list).
2. **Monotone in budget**: result score at budget B2 ≥ score at B1 < B2 (keep-best gate; tested on `coldfire`).
3. **No silent loss**: every connection ends as routed, or in `failed[]` with a reason and a diagnosis, or in
   `skipped[]`/`removed_after_drc[]`. Nothing is removed that was not blamed by an exact check or a DRC marker.
4. **No new DRC in the returned board unless reported**: if a marker survives the repair rounds, the blamed
   connection is removed (and reported) rather than shipped; a residual that cannot be blamed is reported in
   `drc.new_top`.
5. **All-or-nothing writes** with checkpoint/restore, `dry_run`, `compare`, `WORSE_THAN_BEFORE` — inherited from
   `session.py` unchanged.

Not guaranteed: 100 % completion; agreement of the oracle with KiCad on rule types it does not model (list in §4.1);
that auto-detected roles/buses are right (they are reported and can be vetoed).

Acceptance targets, measured only by `bench/route/run.py --router v2 --runs 2` (harness scoring, not the router's own
report). Baseline column is measured; the others are gates for each build stage (§12).

| Metric (14 boards) | Baseline (measured) | Stage 1 gate | Stage 3 gate | Stage 5 gate | Final target |
|---|---|---|---|---|---|
| Connections done | 81.8 % | > 81.8 %, target ≥ 86 % | ≥ 92 % | ≥ 96 % | ≥ 97 % |
| Boards complete and clean | 6 | ≥ 6 | ≥ 8 | ≥ 10 | ≥ 11 |
| New DRC errors (sum) | 5 | ≤ 5, target 0 | 0 | 0 | 0 |
| Any board below its baseline done % | – | none (hard) | none (hard) | none (hard) | none (hard) |
| The 6 clean boards still 100 % clean | – | hard | hard | hard | hard |
| Deterministic (same twice) | 14/14 | 14/14 | 14/14 | 14/14 | 14/14 |
| `coldfire` at 600 s ≥ `coldfire` at default | no (70.9 < 90.2) | yes (hard) | yes | yes | yes |
| Vias vs human on common nets | up to 2.3× | – | ≤ 1.5× | ≤ 1.3× | ≤ 1.25× |
| Odd-angle segments | 19–39 % | – | < 10 % | < 5 % | < 3 % |
| Segments < 0.2 mm | 18–40 % | – | < 10 % | < 5 % | < 3 % |
| Router wall time per board, default budget | 3–208 s | ≤ 1.2× baseline | ≤ 180 s | ≤ 180 s | ≤ 180 s |

If a gate is missed the stage does not become the default path; the previous stage's behaviour stays selectable via
`global.stages`.

---

## 2. Architecture

```
session.route(pr, plan, ...)            (unchanged contract; dispatches on cfg.engine)
  └─ v2.engine.route(...)
       extract (worker) → guided.model.Board → Oracle
       S0 classify   roles, widths, buses, connections (Steiner order)
       S1 power      dog-bone sites, thermal capacity, decoupling stubs
       S2 fanout     fine-pitch escapes → escape terminals
       S3 global     tile graph, PathFinder to convergence, layer DP → corridors   ← dry_run can stop here
       S4 detail     pattern L/Z → window A* in corridor → bounded widening
       S5 repair     probe → min-cut rip → permutation → rescue → global feedback → certificate
       S6 polish     pull-tight, via removal, pad entry, re-widen
       S7 judge      commit → refill → DRC → blame → local re-route (≤4) → keep best → pours pass
       diag          stages[], congestion, cuts[], critique[], suggest[], overlays
```

Each stage is `run(ctx, budget) -> StageReport` operating on one `Ctx` (board, oracle, route state, config). After
each stage the engine compares `state.score()` with the best snapshot and restores the best if the stage made it
worse (S1–S3 are exempt from the "opens" term because they do not close connections; they are gated on reservations
being legal).

Units: mm as Python floats, exactly as v1. Every emitted coordinate is quantised to 1 nm (`q(v) = round(v*1e6)/1e6`)
**before** the oracle checks it, so the checked geometry is the geometry KiCad stores.

Server rules kept: stdlib + Pillow, no LLM call, no in-process pcbnew, all board access through
`pcb/workers/guided_board.py` ops `ripup|fill|extract|commit|remove|stats`. No worker change is required by v2.

---

## 3. Shared data structures (`v2/types.py`, frozen before parallel work starts)

Frozen by W0 as implemented in `v2/types.py` (tests: `tests/test_v2_types.py`). Fields after the first group of
each record have defaults, so records can be built with keywords only.

```python
TOL_MM   = 0.0003     # legal iff dist >= need - TOL_MM. MEASURED in W0 (Appendix A2): KiCad 10 flags iff the gap is
                      # more than 0.5 um short; 0.4 um is the largest tolerance with zero false-legal, minus 0.1 um for
                      # the extract's rounding. The first guess of 0.001 gave 124 false-legal results on the ladder.
                      # Applies to clearance / hole / edge only; keep-out and mask rules get no tolerance.
GUARD_MM = 0.002      # extra inflation used only when rasterising a window grid (a floor: see oracle.raster_guard, A1)
PROTECT_FACTOR = 8.0; WORK_PER_SECOND = 200_000; STAGE_SHARE = {...}; CHARGE_* ; ROLES; ROLE_ORDER   # constants
def q(v: float) -> float                      # quantise to 1 nm

@dataclass(frozen=True)
class Piece:          # one straight segment or one through via
    id: int           # monotone, assigned by RouteState
    net: str; conn: int            # conn = -1 for reservations not tied to a connection
    kind: str         # 'seg' | 'via' (anything else raises ValueError)
    layer: int        # seg only (board layer index); -1 for via
    x1: float; y1: float; x2: float; y2: float   # via: x1,y1 only (x2 == x1, y2 == y1)
    w: float          # seg width | via size
    drill: float = 0.0             # via only
    stage: str = "detail"          # 'power'|'fanout'|'detail'|'repair'|'polish'|'legacy'
    protected: bool = False        # escape stub / dog-bone: rip price × PROTECT_FACTOR
    target_w: float = 0.0          # width polish should try to restore; 0.0 is replaced by w (== w when not necked)
    length -> float                # property: segment length, 0.0 for a via

@dataclass(frozen=True)
class Terminal:
    x: float; y: float; layers: tuple[int, ...]
    pad: str | None = None; cluster: int = 0; escape: bool = False

@dataclass
class ConnV2:
    idx: int; net: str
    a: Terminal; b: Terminal
    est: float                     # octile estimate, mm
    role: str                      # 'plane'|'power'|'pair'|'bus'|'critical'|'signal'
    width: float; neck: float; clearance: float; via: tuple[float, float]
    rank: tuple                    # canonical order key
    tree_target: bool              # may tee onto any same-net piece

@dataclass
class Corridor:
    conn: int; tiles: frozenset[tuple[int, int]]      # 2-D tile coords, already dilated by 1 ring
    layers: tuple[int, ...]; layer_seq: tuple[tuple[int, int, int], ...]   # (tile_i, from_layer, to_layer) via hints
    lateral: dict                  # (tile_edge) -> rank among crossings (planarity hint)
    overflow: float                # residual overflow on its path (0 = feasible by the model)

@dataclass
class Blocker:
    rule: str          # 'clearance'|'hole'|'edge'|'keepout'|'mask'|'short'|'thermal'|'reserve'
    net: str | None; tag: str | None       # tag = 'U3.4' (pad), 'via', 'track', 'edge', 'hole', 'keepout', 'mask',
                                           #       'zone', 'copper' (graphic), 'soft:<id>'
    static: bool; piece: int | None        # piece id when dynamic copper (then static is False)
    dist: float; need: float               # actual / required gap, copper edge to shape (dist < 0 = overlap)
    at: tuple[float, float]; layer: int    # at = point on the checked item nearest the blocker

@dataclass
class Cut:
    a: str; b: str; layer: str             # pad refs or 'edge'/'tile(x,y)|E'
    capacity_mm: float; demand_mm: float; nets: list[str]

@dataclass(order=True)
class Score:          # compared lexicographically, smaller is better; all fields default to 0; key() -> tuple
    new_errors: int; opens: int; necked_mm: float; vias: int; length_mm: float; bends: int

@dataclass
class StageReport:
    name: str; routed: int; failed: int; score: Score; work: int; notes: list[str]; data: dict

class Budget:         # deterministic work units, same scale as v1 (time_budget_s × 200 000)
    def __init__(self, total: int, parent: "Budget | None" = None): ...
    total: int; used: int
    def charge(self, n: int) -> None          # charges this budget and every ancestor; n <= 0 is ignored
    def left(self) -> int                     # max(0, total - used)
    def sub(self, fraction: float, floor: int = 0) -> "Budget"
        # child of max(floor, min(int(fraction × total), left())) units. An unused share therefore rolls forward to
        # whoever asks next; floor is a guaranteed minimum even when the parent is spent out (the parent goes over).
    def spent_out(self) -> bool               # used >= total
# charges: 1 per A* expansion (tile or cell); 4000 per window search started; 2000 per window build;
#          1 per 8 oracle checks; 200 per pattern attempt. No wall-clock anywhere.

@dataclass
class Ctx:
    board: "guided.model.Board"; cfg: "guided.plan.Config"; x: "planx.V2Cfg"
    oracle: "Oracle"; state: "RouteState"; conns: list[ConnV2]
    corridors: dict[int, Corridor]; tiles: "Tiles | None"
    history: dict; reports: list[StageReport]; progress: Callable | None
```

Stage budget shares of the search budget (unused share rolls forward): power 3 %, fanout 5 %, global 10 %,
detail 45 %, repair 22 %, polish 10 %, judge re-routes 5 %.

---

## 4. Stage algorithms

### 4.1 Oracle (`oracle.py`) — the single legality test

Wraps `guided.model.Board` and its `geom.ShapeIndex` (which already holds exact pad/track/via/zone/outline shapes).
Adds a dynamic layer for v2 pieces and a soft layer for reservations.

`need(a, b)` — required gap between a new item (net, half-width, kind) and an existing shape, the maximum of:
net-class clearance of both nets and the board minimum; pad-specific clearance override; hole-to-copper clearance for
drilled pads/vias and **NPTH**; hole-to-hole for via vs hole; copper-to-edge clearance for outline shapes; for
foreign pads on outer layers, `mask_margin + min_mask_web` (solder-mask bridge); keep-out areas (infinite need for
tracks/vias as flagged). Same-net copper has need 0 (tees allowed). A foreign-net overlap is a `short`.

Legal iff `dist >= need - TOL_MM` on the quantised geometry.

**As built and measured in W0** (each line checked against kicad-cli 10.0 on the seeded board, Appendix A2; where
this differs from the paragraph above, this list is the contract):

- `dist` and `need` are gaps between copper edges (the centre line stays `half_w + need` away). Same net: no
  constraint, `need()` returns `NO_NEED` (−inf), not 0.
- Clearance: `max(class of net a, class of net b, board minimum)`. A pad with a clearance override — its own, else its
  **footprint's** — uses `max(override, board minimum)` *instead of* the classes, even when that is smaller.
- The two nets of a **coupled differential pair** (KiCad's suffix rule: `+/-` or `P/N`, optionally followed by digits
  and underscores) use the class's pair gap when it is below the class clearance.
- Hole: copper to a foreign or NPTH hole ≥ hole clearance; a via's own drill to foreign copper likewise (a thin-annulus
  via needs more than its copper clearance); via drill to any **round** hole, same net or not, ≥ hole-to-hole.
- Edge: measured to the outline's centre line (the line width of the Edge.Cuts graphic does not count); a point outside
  the outline is always illegal.
- Keep-out: need **0**, not infinite (copper may touch the boundary, not enter), and **no tolerance**. No-track areas do
  not bind vias and vice versa; a keep-out on any layer binds a through via.
- Mask: foreign pad on an outer layer: `pad mask margin (pad, else footprint, else board) + min web / 2`, no tolerance.
  Graphics on the mask layers stay hard keep-outs for every net (v1 behaviour; stricter than KiCad, which allows one net
  under an opening — `Oracle.mask_openings_hard`).
- `TOL_MM` applies to clearance / hole / edge only (KiCad's 0.5 µm epsilon exists only there).
- Footprint-level overrides and mask margins are not in the worker's extract: `oracle.local_overrides_from_file(pcb)`
  reads them from the board file text and `Oracle(board, overrides=...)` applies them. Without it footprint overrides
  and the mask rule are not applied.

Soft shapes (never make copper illegal, they return a `Blocker(rule='thermal'|'reserve')` that callers price):
thermal-spoke corridors of pour pads and reserved via sites.

Not modelled, therefore left to S7 and listed in `notes`: custom `.kicad_dru` conditional rules beyond the minimums
`model.dru_minimums` already reads, zone-fill outcomes (starved thermals are predicted by the spoke shapes but only
KiCad's refill decides), silk and courtyard rules, length/skew rules.

**W0 gate (differential test)**: for each of the 14 corpus references (human-routed) the oracle must judge every
reference track and via legal wherever kicad-cli DRC reports no copper error on it, and must flag every item KiCad
flags among `session.COPPER_TYPES`. Plus seeded violations: copies of reference segments shifted to give gaps of
`need + {+2, 0, −0.5, −2, −6} µm` establish KiCad's real epsilon; `TOL_MM` is then set to the largest value with zero
false-legal results. Zero disagreements is the gate for anything in v2 that trusts the oracle at the rule distance.

*Result (W0, Appendix A2)*: 12 references (the two synthetic boards have none), 14 243 items: **0 false-legal**; 399
flagged by both, for the same rule; 80 flagged by the oracle only, all on the strict side with a known reason.
Seeded ladder, 34 cases × 21 deltas: 0 false-legal at `TOL_MM = 0.0003`. Rerun: `bench/route/oracle_diff.py`,
`tests/test_v2_oracle.py [--full]`.

**Pre-work**: trace the `one_air_max` `_verify` failure at (126.51, 81.42) (suspected: snap/escape stubs bypassing
the exact check). The finding is written into this doc's appendix and turned into an oracle test.

*Result (W0, Appendix A1)*: the suspicion was wrong. The failing segments are on-grid 45° steps past a foreign pad
corner; the grid's fixed EPS is smaller than the dip of a step between two free cells at that pitch.

### 4.2 RouteState (`pieces.py`) — transactional copper

Pieces are stored by id with indexes by connection and by net. `add`/`remove` update the oracle's dynamic layer.
A transaction is an undo log (`begin() -> token`, `rollback(token)`, `commit(token)`); transactions nest.
`snapshot()` is an immutable tuple of pieces plus the connection status map; `restore()` diffs against the current
set. `connected(conn)` uses a per-net union-find over piece endpoints, pads and tee points (endpoint on a same-net
segment within 1 nm). `score()` computes `Score` from the state (`new_errors` is set by the judge).

### 4.3 S0 classify (`classify.py`)

- Role per net, first match: plan `role`; has a pour/zone → `plane`; `plan._GROUND`/`_POWER` name or net class named
  power → `power`; declared pair → `pair`; bus member → `bus`; crystal/decoupling (existing `keep_short`) →
  `critical`; else `signal`.
- Bus detection (conservative): ≥ 4 nets whose pads are consecutive, in the same or exactly reversed order, on two
  footprints; never auto-detected across more than two footprints. Every auto role/bus/width is listed in
  `auto{}` of both `pcb_route_plan` and the result so the caller can veto it.
- Width: plan `width`; else for `power` `min(class_w × power_width_factor (default 2.0), 0.8 × smallest pad minor
  dimension on the net, 1.0 mm)`; `neck` = class width. Others: class width.
- Pourless grounds (GNDA) are routed as `power`, late, never skipped. v1's "skip GND without pour" stays only when
  the plan asks for it.
- Connections: incremental Steiner order per net. Start from the largest cluster; repeatedly attach the nearest
  unconnected cluster; each `ConnV2` has `tree_target=True` so its target is the whole routed tree of the net.
  `plan tree: star|chain|mst` overrides.
- Global order key `rank`: (plan rank, −priority, role order critical < pair < bus < signal < power < plane,
  est, net name, idx).

### 4.4 S1 power (`power.py`)

For every SMD pad of a `plane` net not on a layer where its pour reaches it:

1. Candidate via sites: 8 directions × 3 stub lengths (pad edge + via radius + clearance, +0.3 mm, +0.8 mm), plus
   plan `reserve` sites, in canonical order.
2. Score = stub length + 2 × tile congestion estimate + **escape-lane penalty** (site lies in the perpendicular
   exit lane of a signal pad of the same footprint, or in a pad-pair cut) + penalty if the via lands on a pour
   island boundary.
3. First candidate passing `oracle.seg_ok` and `via_ok` is added as `protected` pieces (`stage='power'`). If none
   passes, the pad is recorded as `power_unreserved` and retried in the pours pass (S7).

Thermal spokes: for each through-hole or SMD pad on a pour layer, 4 spoke corridors (spoke width from zone settings
when present in extract, else 0.5 mm) are added as soft shapes and booked as capacity in the tile graph
(`Tiles.book`). Detail search prices them (×6), repair may cross at most 2 of 4.

Decoupling: a 2-pad capacitor whose pads are on a power net and a plane net, within 3 mm of an IC pin of the same
power net, gets its cap-to-pin connection routed first at power width with the pattern router.

Non-pour power nets are ordinary connections with `role='power'` routed late at full width; necking is per segment
(§4.9 step 4), never per connection.

Reservation rip rule (used by S5): a `protected` power piece may be ripped only inside a transaction that re-places
the dog-bone for the same pad at another legal candidate; otherwise the transaction rolls back.

### 4.5 S2 fanout (`fanout.py`, `flow.py`)

Applies to footprints where `pitch < 2 × (w + cl) + pad_w` or pad count ≥ 16 (and `fanout != 'none'`).

Per pad row (QFP/QFN/SOIC/connector):

- Exit direction is the outward row normal. The escape is generated **analytically** and checked only by the oracle
  (this is what makes the 0.4 mm-pitch, at-the-rule QFN case legal): a straight stub from pad centre outwards to
  the first ring radius, then optionally a 45° leg to a slot.
- Slots lie on rings parallel to the row at distance `pad_len/2 + k × (w + cl)`, k = 1..3, spaced `w + cl`. Slot
  order equals pad order (planar by construction). Fan direction per pad is chosen by the bearing to its partner
  terminal; a pad whose partner is straight ahead keeps the straight stub.
- Pads whose partner is on another layer, or rows whose ring demand exceeds the ring capacity, escape **inward or
  outward to a via** by min-cost flow: nodes = pads, pad-pair cuts (capacity = `Tiles.pad_cut` tracks) and
  candidate via sites; cost = stub length + congestion; solved by successive shortest paths (`flow.py`). Via-site
  capacity is per column because vias are through-only.
- Each escape is committed as `protected` pieces (`stage='fanout'`) and the connection endpoint becomes an
  **escape terminal** (`Terminal.escape=True`) on the window lattice-compatible point at the stub end.
- A pad with no legal escape is reported at once as `boxed in` with its pad-pair `Cut` — before any search budget
  is spent on it or its partner.

Plan `fanout: {ref: {style: out|in|via|auto, side, layer}}` overrides. BGAs get only dog-bone-style via escapes
(no Yan–Wong); a BGA that needs more is reported in `critique[]`.

### 4.6 S3 global (`tiles.py`, `globalroute.py`)

**Tile graph.** Uniform square tiles per board: `T = 1.0 mm`; 0.5 mm if the board has pad pitch ≤ 0.5 mm; then
doubled until `tiles × layers ≤ 60 000`. Node = (layer, tx, ty). Edges: 4 neighbours in-layer, and a via edge
between all layer pairs of a column.

**Capacity from exact geometry.** For each tile border on each layer, subtract from the border segment every static
shape inflated by its `need` (via `oracle.near`) to get free intervals `len_i`. With reference pitch
`p = w_ref + cl_ref` of the default class: `cap_mm = Σ floor((len_i + cl_ref) / p) × p × region.capacity_scale`.
Demand of a connection on an edge is `w + cl` of that connection. Via capacity of a column = number of legal via
sites on a `via + cl` lattice inside the tile (legal on **all** layers), minus reservations. Thermal spokes and
dog-bone sites are booked as demand.

**Pad-pair cuts.** For neighbouring pads of one footprint and facing pads of adjacent footprints (gap ≤ 3 mm):
`tracks = floor((gap − cl) / (w + cl))`, also for the diagonal (vertex) gap. Used by fanout feasibility and as the
named `Cut` in certificates.

**Calibration gate (offline, part of W3).** On the 14 human-routed references, count reference tracks crossing each
tile border; `cap_mm × CAL` must be ≥ observed demand on ≥ 99.5 % of borders. `CAL` (start 1.0) is the single
calibration constant; the measured distribution goes in the appendix. If the gate cannot be met with
`0.8 ≤ CAL ≤ 1.3`, corridors are treated as advisory (cost only), not as masks.

**PathFinder.** Connections in `rank` order, A* on the tile graph (heuristic: Manhattan tile distance to the nearest
target tile; targets = all tiles of the net's tree when `tree_target`).

```
cost(edge) = len × (1 + hist[edge]) × (1 + pres × over(edge)) / (1 + share)
           + wrong_dir × layer_dir_penalty + plan multipliers (corridor / avoid / between / region)
cost(via)  = via_cost_mm × (1 + pres × over(column)); infinite where region.no_vias or max_vias reached
```

Iteration 0 is congestion-blind (`pres = 0`); then `pres = 0.5 × 1.5^k`, `hist += over` after each iteration; only
connections using an overflowed edge are re-routed; stop at zero overflow, at 30 iterations, or on budget; the
iteration with the smallest (total overflow, wirelength) is kept.

**Layer directions** when the plan gives none and the board has ≥ 2 signal layers and ≥ 40 connections: histogram
of connection bearings weighted by length; the dominant axis goes to the layer with more free capacity along it,
the other layer gets the orthogonal one; soft penalty 1.3.

**Layer-assignment DP (via planning).** For each connection's 2-D tile path, a Viterbi pass over
(path position, layer) minimises `Σ edge cost on that layer + via_cost × layer changes`, endpoints constrained to the
terminal layers, respecting `layers_ok`, `max_vias`, `via_budget`. Output `layer_seq`.

**Output** per connection: `Corridor` (path tiles + 1 ring, layers, via hints, lateral ranks = order of crossings on
each border sorted by projected endpoint coordinate, residual overflow). Residual overflow is reported as `cuts[]`
immediately; with `dry_run` and `stages` ending at `global` the run stops here (no detail work spent).

**As built and measured in W3** (Appendix A3; where this differs from the paragraphs above, this list is the
contract; the reasons are measurements, not preferences):

- Capacity: `raw_cap = Σ floor((len_i + cl_ref) / p) × p` as above, and `cap = (raw_cap + slack) × CAL × scale −
  booked` with `slack` = the largest remainder `(len_i + cl_ref) mod p` of the border (0 when no reference track
  fits). Slack never admits another reference track; it stops one track wider than the reference class from
  overflowing a border it fits through (tracks of widths w_j fit a free interval iff `Σ (w_j + cl) ≤ len + cl`).
  A free interval cut by a tile corner is counted together with its continuation in the next border.
- Borders in the shadow of a connection's own terminal pads carry no demand of that connection. Only static geometry
  is subtracted; reservations (protected copper, thermal spokes, reserved sites) are booked as demand.
- Via capacity: legal share of a tile-anchored sample lattice × T² / (via + cl)², at least 1 when any sample is legal.
- `CAL = 1.0` passes the gate when reference tracks are **counted** (99.97 % of all borders, 99.50 % of the borders a
  reference track crosses, 12 boards pooled); weighted by the human track widths it does not on used borders
  (95.7 %). Corridors are therefore masks (`CORRIDORS_ADVISORY = False`) with that caveat.
- Crossings are modelled (the design had only layer directions): an exact test on shared tile runs after every
  iteration (overflow, history, both connections re-routed) and a cheap price inside the search. Without it a
  2-layer bus crossing converges with zero vias planned.
- The search aims at the nearest tile of a target tree, uses v1's heuristic weight and a box around the terminals;
  the layer DP refuses a re-layering that adds overflow or a crossing.
- A `tree_target` connection ends only on tile paths of connections that precede it in `rank`
  (`history['global']['tee']`). Pad-to-pour connections (open terminal b) get no corridor.
- `Corridor.layer_seq = ((i, from, to), ...)` indexes `history['global']['path'][idx]` (= `corridor.path`).

### 4.7 Window grid (`window.py`) and search (`search.py`)

**Window** = bounding box of the corridor tiles + 1 mm, one `guided.grid.Grid` over that box.

Pitch `p`: the largest value in `{0.25, 0.2, 0.15, 0.125, 0.1, 0.075, 0.05}` such that (a) `p ≤ (w + cl) / 2`,
(b) the minimum pad pitch among fine-pitch footprints touching the window is an integer multiple of `p` within
1 µm when any such `p` exists, otherwise `p ≤ min_pitch / 4`, (c) `cells × layers ≤ 400 000`; if (c) fails the
window is split at via hints or `p` is coarsened one step. Origin is shifted so the dominant pad-centre residue
(mode of `x mod p`, `y mod p` over pads in the window) falls on cell centres.

Build: cells outside the corridor mask are blocked; every shape from `oracle.near` is rasterised with
`grow = need + half_width − TOL_MM + GUARD_MM` using `Grid.shape_spans/stamp` into a `ClassMap` for this
connection's probe class (**W0 finding, A1**: a fixed guard does not make steps between free cells legal — the dip of
a diagonal step past a corner is `oracle.raster_guard(p, half_width + need)`, e.g. 23.7 µm at p = 0.15 mm, reach
0.225 mm; use `max(GUARD_MM, raster_guard(...))` or expect the emit-time oracle check to reject such steps); dynamic pieces are stamped into a separate owner array (hard in normal search, priced in
soft search). **Every window is rasterised fresh from the oracle**, so windows of different pitch and origin never
share lattice state and cross-window consistency is the oracle's job. A small LRU (8 windows keyed by box, pitch,
class, state version in box) avoids rebuilding during repair.

Cost gate: window build must be ≤ 30 % of detail-stage work on the benchmark; this is unmeasured today and is the
first thing W4 measures. Fallback if it fails: per-tile raster cache.

**Search** is a v2-owned A* (forked from `guided/astar.py`, which stays untouched): 8 directions, through vias,
multi-source/multi-target; heuristic `hweight × max(octile distance to the nearest of ≤ 8 sampled target cells,
tile-level reverse-Dijkstra distance inside the corridor − T√2)`; costs: bend table (`astar.turn_table`), via cost,
per-cell multipliers (plan regions, pad repulsion within 1 cell of foreign pads, thermal/reserve prices, lateral
hint bias), history; optional `bend_state=True` (incoming direction in the state) only when the window has
≤ 60 000 cells; optional soft mode where foreign dynamic cells cost `soft_cost[piece]` and are recorded as crossed.

### 4.8 S4 detail (`detail.py`)

Per connection in `rank` order (re-sorted by corridor overflow, most constrained first, within equal plan rank):

1. **Pattern**: if both terminals share an allowed layer, try the ≤ 6 direct shapes (straight, two L postures with a
   45° corner, two Z postures) with the oracle. First legal one wins. Cost: 200 units each.
2. **Probe**: 2 000-expansion flood from each terminal in a small window. An enclosed terminal → fail fast with
   reason `pad boxed in`, the frontier's blockers and `static_share`; goes to repair without paying a full search.
3. **Window A\*** in the corridor at full width (`limit = min(120 000, 20 000 + 1500 × est / p) × effort`).
4. Same at `neck` width (the result keeps `target_w = width`; polish widens per segment).
5. Corridor + 2 rings, both widths.
6. **Bounded wide search**: box of both terminals grown by 25 % + 5 mm, hard cap `40 000 × effort` expansions.
   There is no unbounded full-board search anywhere in v2.
7. Hand to S5 with the best failure record.

Geometry: cell path → 45° polyline with collinear merge; terminal joins are an axis-aligned or 45° segment into the
pad centre or onto the tee point; all points quantised; **every** segment and via is checked by the oracle
(joins and on-grid runs alike — the measured v1 gap is an on-grid diagonal step, A1). A failing segment blocks its cells in the window and re-searches
(≤ 3 times); then the connection fails alone. Success → `state.add` inside a transaction.

### 4.9 S5 repair (`repair.py`)

For each failed connection in `rank` order, transactional, bounded by the stage budget and a tabu set of
`(conn, frozenset(victims))`:

1. **Min-cut rip**: soft search in the corridor + 2 rings. Rip price per foreign connection
   `= 4 mm × (w / w_min) × (PROTECT_FACTOR = 8 if protected) / max(1, routed_len / est) + history`.
   Rip exactly the crossed connections, route the failed one, then re-route victims through S4 steps 1–6. Accept if
   `state.score()` improved; else roll back and add history 0.3 mm on the contested cells.
2. **Cluster permutation**: connections failed or ripped twice within a 12 × 12 mm box, ≤ 5 of them: rip all, try
   orders in lexicographic order (≤ 120) under a per-cluster cap, keep the best; exhausting it without success is
   recorded as `"these nets cannot share this channel"` with the tightest `Cut`.
3. **Rescue**: half window pitch; one extra via above `via_budget`; neck width; alternate fan-out side (rip only
   this connection's own escape stub and re-escape, in the same transaction).
4. **Global feedback**: decrement the capacity of the tile borders named by the failure by the failed demand,
   re-run S3 for connections crossing them, then S4 for those. At most 2 feedback loops per board.
5. **Certificate**: `failed[]` entry with `reason` (v1 phrases kept: "boxed in", "effort", "time budget",
   "outside the board outline", "max_"), `cut`, `blocked_by` ranked, `static_share`, `mincut_nets`, `suggest[]`.

Stage-1 note: while S4 is not built, `legacy.py` supplies detail routing (v1 `Router`), and repair is v1's own.

**As built (W7; details and measurements in `docs/internal/v2-interface-notes.md`, section W7; where this differs from the list
above, this is the contract):**

- The ladder runs rung by rung over all failed connections (not connection by connection), and rescue runs *before*
  min-cut and permutation (`repair.RESCUE_FIRST`): it disturbs nobody and, measured, late connections otherwise
  starved. Rescue rungs, cheapest first: one extra via with the global layer assignment lifted; half pitch; neck
  width; the other fan-out side. A step 0 was added: an exact ray probe of both terminals (`repair.probe`).
- A rip is accepted when the score improved **and** every victim (and every connection that teed onto one) is routed
  again. A victim gets a second try with the layer assignment lifted. After a rip that did not pay the blamed
  victims are hard for that connection's next soft search (≤ 3 soft searches per connection), plus the history.
- Protected copper: a dog-bone moves only with `power.place_dogbone(..., exclude=old sites)`; an escape stub only
  when every connection of its pad can be ripped and `fanout.re_escape` finds another escape.
- A cluster is the failed connection, other failed ones in the box, its min-cut victims and what *they* fight, and
  connections ripped twice. Orders are walked as a tree (shared prefixes routed once).
- Failures no search can repair (`outside`, `layer_pad`) skip the ladder.
- The certificate also carries `probe`, `tried`, `ladder`, `blockers`; `cut` is None when no over-capacity cut was
  found (diag derives its own).

### 4.10 S6 polish (`polish.py`)

Every edit is a transaction, oracle-checked, connectivity-checked (`state.connected`), and kept only if `Score` does
not get worse. Passes in order, each to a fixed point or budget:

1. **Pad entry**: last segment into a pad is axial or 45° and ends on the pad centre; stubs < 0.2 mm merged.
2. **Pull-tight**: for each polyline, try replacing point runs `i..j` by the two 45° postures between them, longest
   span first, keeping the side of every obstacle (keep-topology test: the swept polygon between old and new path
   contains no foreign shape).
3. **Staircase removal**: three consecutive segments with two bends < 0.6 mm apart → one diagonal or one L.
4. **Via removal**: for each pair of vias joined by a span on another layer, try the span on the entry layer with
   pattern/window search at `max_vias = 0`; accept if length grows < 1.3×.
5. **Re-widen**: for each piece with `w < target_w`, bisect the widest legal width in [w, target_w] (0.01 mm
   resolution); long necked segments are split at the legality boundary so only the tight span stays necked.

A minimal version (passes 1 and 3 only) ships at build stage 2 and runs on legacy output.

**As built (W8; where this differs from the list above, this is the contract — details in
`docs/internal/v2-interface-notes.md`, W8):**

- An edit = remove a run of one connection's pieces + add new ones in one transaction. Kept only if every new
  segment is `oracle.seg_legal`, crosses no soft shape the old copper did not cross, stays out of forbidden plan
  regions, keeps the side of every obstacle (same-layer edits), and `state.connected` still holds for the connection
  and for every same-net connection that touches it. Tee points, shared end points and teed segments never move.
- Score rule: per edit "not worse", **except clean-up edits** (fewer odd-angle / < 0.2 mm segments and no new ones;
  a track end moved onto its pad centre), which may cost length while the *stage's cumulative* Score delta stays not
  worse (`Score` has no odd-angle term, and a 45° posture is often longer than the odd stub it replaces). After
  every pass the whole state is audited against the stage-start score and connectivity; a failing pass is restored.
  `ctx.x.polish_slack_mm` (default 0) allows that much total length growth for clean-ups.
- Every geometry pass first removes dead ends / loops that lead nowhere and merges collinear neighbours.
- Pass 3 also turns a 90° jog between parallel segments into a 45° jog when the full L is blocked, and replaces
  interior odd-angle and < 0.2 mm segments by 45° postures.
- Pass 4 tries patterns only (straight, 2 L, 2 Z, the same polyline on the entry layer), no window search; it also
  removes a single via in front of a through-hole pad.
- Pass 5 splits only axial / 45° segments that nothing tees onto (0.1 mm boundary resolution, wide runs ≥
  max(2 × target width, 0.3 mm)); other necked pieces get the widest legal width as a whole.
- Not touched: `protected` pieces, `role == 'pair'`, connections that are not routed and connected, `conn == -1`.
- Not modelled: zone fills. Without S1's thermal soft shapes polish can starve a thermal relief or cut a pour
  neck (measured: `bench/route/polish_bench.py --drc`); S7 judges after polish.

### 4.11 S7 judge (`judge.py`, `kio.py`)

```
best = snapshot
for round in 0..4:
    commit state to scratch board (tracks/vias, uuids returned in input order) → fill → DRC
    new = new errors vs baseline (session._new_errors);  state.new_errors = len(new)
    if score better than best: best = snapshot (with its scratch file)
    if not new: break
    for marker in new (canonical order):
        blamed = pieces whose uuid is in the marker, else pieces within 0.3 mm of the marker
        remove only the blamed connections (never restart, never rip a net)
        history += 1.0 at marker (radius 1 mm), all history × 0.95 per round
        starved_thermal → add the pad's spoke corridors as hard shapes for foreign nets
    re-route removed connections through S4/S5 inside the judge budget
restore best
if best.new_errors > 0: remove the blamed connections once more, report them in removed_after_drc, re-verify
pours pass (≤ 2 rounds): pour pads still unconnected after refill get their reserved dog-bone / a new one
```

The session-level write (checkpoint, all-or-nothing, `WORSE_THAN_BEFORE`, `compare`, `dry_run`) stays in
`session.py`.

**As integrated (stage 1)**: a `starved_thermal` marker that names a spoke count ("min spoke count N; actual K") is
first blamed at spoke level — only the connections closing the `N − K` cheapest spoke corridors are removed and only
those corridors (plus the ones still free) are closed to other nets; the pad's whole neighbourhood is the second
step, removal the third. When no repair stage runs, the judge's re-routes also get the repair stage's budget share
(as a floor). Details: `docs/internal/v2-interface-notes.md`, "Integration — build stage 1".

---

## 5. Why completion should approach 100 % (and where it will not)

| Measured loss (baseline) | Mechanism in v2 | Stage |
|---|---|---|
| Whole-net rip on verify (75 conns `one_air_max`, ×87 `coldfire`) | connection-level verify, joins checked | 1 |
| Destructive DRC loop (26 conns `interf_u`, `coldfire` regression) | S7 blame-local repair + keep-best | 1 |
| Budget exhaustion on big boards (193 conns never tried) | corridors + windows; bounded wide search | 3 |
| At-rule fine pitch (33–51 boxed-in + 18 partners, `tinytapeout`) | oracle `TOL`, analytic escapes, lattice-snapped windows | 2–4 |
| Pour pads walled in (≈ 76 conns) | S1 reservations before signals | 2 |
| Fan-out/bus blocking, USB pairs | S2 ordered fans, S5 connection-level rip | 4–5 |
| Unmodelled rules (NPTH, thermals) | oracle `need`, soft spokes | 0–2 |
| GNDA skipped | routed as power | 2 |

Expected residue: boards whose placement leaves a cut over capacity (reported as such), inner layers that are
≥ 40 % pour (still excluded by default; plan can enable them), and rule types outside the oracle.

---

## 6. AI steering interface

The server never calls a model. The calling model reads `pcb_route_plan`, writes a plan, optionally runs a
stage-limited `dry_run`, reads diagnostics, patches the plan, and runs again.

### 6.1 Plan fields (all optional; defaults reproduce un-steered behaviour; unknown fields still raise `BAD_PLAN`)

Existing fields keep their names and meaning. `planx.py` must first reconcile this table against
`PLAN_FIELDS/GLOBAL_FIELDS/NET_FIELDS/REGION_FIELDS`; where an existing field already expresses a hint (width,
corridor, avoid, layer directions, neck widths, max_vias) it is reused and no new name is added.

| Scope | Field | Meaning |
|---|---|---|
| global | `engine: "guided" \| "v2"` | engine switch; default `guided` until the final gate |
| global | `stages: [...]` | subset/prefix of `classify, power, fanout, global, detail, repair, polish, judge` |
| global | `tile_mm`, `negotiation_iters`, `power_width_factor`, `fanout: auto\|none`, `polish: bool` | tuning |
| net | `role` | `plane\|power\|pair\|bus\|critical\|signal` |
| net | `protect: bool` | never rip this net's copper |
| net | `via_budget: int` | soft target for the layer DP; `max_vias` stays the hard limit |
| net | `waypoints: [[x, y, layer?]]` | forced tile visits in order |
| net | `between: [["U3.4","U3.5"]]` | route through that pad-pair cut (tile-edge cost × 0.2) |
| net | `side_of: [{ref, side: cw\|ccw}]` | pass the part on that side (half-plane cost × 5 on the other) |
| net | `tree: star\|chain\|mst` | decomposition override |
| top | `buses: [{nets, order?, layer?, spacing?}]` | declared bus; members share a corridor, lateral ranks follow `order` |
| top | `fanout: {ref: {style, side?, layer?}}` | escape style per part |
| top | `reserve: [{at: [x,y], for: net}]` | via site reserved for a net |
| region | `capacity_scale`, `no_vias` | tile capacity multiplier, via ban |

### 6.2 Diagnostics (added to the existing result; existing keys unchanged)

- `stages: [{name, routed, failed, score, work, notes}]`
- `auto: {roles, buses, widths, layer_dirs}` — everything the router decided on its own, for veto.
- `congestion: {tile_mm, layers: {name: [[util%...]]}}` (coarse, ≤ 40 × 40 per layer) and
  `cuts: [{a, b, layer, capacity_mm, demand_mm, nets}]` (top 10 by overflow).
- per `failed[]` entry: `cut`, `static_share` (0–1), `mincut_nets`, `probe` (`enclosed: a|b|none`),
  `tried` (ladder steps reached), plus the existing `net, from, to, reason, where, blocked_by`.
- `critique: [{kind, net, value, limit, where}]` — `vias_over_budget`, `necked_share`, `detour` (> 2× estimate),
  `bus_unbundled`, `power_thinner_than_pad`, `odd_angle_share`.
- `suggest: [{why, patch}]` — each `patch` is a ready plan fragment (e.g. `{"nets": {"/X": {"via_budget": 3}}}`,
  `{"regions": [...capacity_scale...]}`), or `{"move": {"ref": "C12", "by": [0.4, 0]}}` for placement.

### 6.3 Images (Pillow; text stays primary)

`diag.overlays()` returns primitives in board mm; the existing render path draws them:
(a) congestion heat overlay with the top cuts labelled; (b) for a stage-limited dry run, the global sketch: one
polyline per connection through tile centres, overflowed borders in red; (c) per failure (max 6), a crop with both
terminals, the frontier reached from each end, the named cut and the blocking nets labelled.

### 6.4 The loop the tool descriptions teach

`pcb_route_plan()` → `pcb_route_guided(plan, dry_run=True)` with `stages` ending at `global` (seconds, returns
`cuts`, `auto`, sketch) → patch plan → full run → read `critique`/`suggest` → targeted `nets=[...]`, `ripup="nets"`.

---

## 7. Determinism

- No `random`, no wall clock in any decision, no iteration over `set`/`dict` of unordered keys without `sorted`.
- Heap entries are `(f, g, seq, state)` with a per-search sequence number.
- Canonical orders: connections by `rank`; pads by (ref, pad number); candidates by generation order; DRC markers
  by (type, x, y, description); pieces by id.
- Floats: all emitted coordinates through `q()`; comparisons that decide control flow use quantised values.
- Budget in work units (§3). Window LRU must not change results (cache hit ≡ rebuild; unit-tested).
- Test: every module has a "same twice" test; the engine has one on two corpus boards comparing commit lists.

---

## 8. Performance budget (targets; baseline throughput 170–290 k expansions/s is measured, the rest is not)

| Item | Budget |
|---|---|
| Oracle check (segment vs neighbours) | ≤ 40 µs median |
| Tile graph build | ≤ 3 s on the largest board |
| Global route | ≤ 60 000 nodes, ≤ 10 s, ≤ 30 iterations |
| Window build | ≤ 15 ms for 50 k cells × 2 layers; ≤ 30 % of detail work |
| Window search | median ≤ 8 k expansions (61 k today on `interf_u`) |
| Polish | ≤ 20 s |
| KiCad round trips | extract 1, commit+DRC ≤ 5, pours ≤ 2 |
| Whole board at default budget | ≤ 180 s wall |

No native accelerator. If the window-build or oracle budgets are missed, the fix is caching, not C.

---

## 9. Module layout — `src/copper_kicad/pcb/v2/`

One owner per file. Interfaces below are the contract; anything not listed is private to the file. All modules may
import `types.py` and v1's `guided.{geom,grid,model,plan}`; other imports are listed.

| File | Owner (WP) | Imports from v2 |
|---|---|---|
| `__init__.py`, `types.py` | W0 | – |
| `oracle.py` | W0 | types |
| `pieces.py` | W1 | types, oracle |
| `kio.py` | W1 | types |
| `legacy.py` | W1 | types, pieces, oracle |
| `judge.py` | W2 | types, pieces, kio, (detail/repair via callables) |
| `engine.py` | W2 | everything (the only orchestrator) |
| `planx.py` | W9 | types |
| `classify.py` | W5 | types |
| `power.py` | W5 | types, oracle, pieces, tiles (optional) |
| `tiles.py` | W3 | types, oracle |
| `globalroute.py` | W3 | types, tiles |
| `window.py` | W4 | types, oracle |
| `search.py` | W4 | types |
| `detail.py` | W4 | types, oracle, pieces, window, search |
| `flow.py` | W6 | – |
| `fanout.py` | W6 | types, oracle, pieces, tiles, flow |
| `repair.py` | W7 | types, pieces, window, search, detail, tiles |
| `polish.py` | W8 | types, oracle, pieces |
| `diag.py` | W9 | types, tiles |

Files outside the package (each touched by exactly one WP):

| File | WP | Change |
|---|---|---|
| `pcb/guided/session.py` | W2 | in `route()`: `if cfg.engine == "v2": return v2.engine.route(...)`-style dispatch for the search/judge part only; checkpoint/restore logic unchanged |
| `pcb/guided/plan.py` | W9 | `GLOBAL_FIELDS += engine, stages`; one call to `planx.extend_schema()` / `planx.resolve()` |
| `tools/route_tools.py` | W9 | `_findings` reads `cut`/`critique`/`suggest`; tool text documents the loop; `pcb_route_plan` returns `auto` |
| `pcb/route.py` (`pcb_route`) | W9 | guided mode passes the plan through unchanged; W9 verifies this by reading it (not read for this design) |
| `bench/route/routers/v2.py` | W1 | adapter (§11) |
| `bench/route/legacy_probe.py` | W1 | diagnostic: pieces + legacy + kio alone on corpus boards (no engine, no judge) |
| `tests/test_v2_*.py` | each WP | one file per module |

### Interfaces

```python
# oracle.py  (as built by W0; everything below is implemented and tested in tests/test_v2_oracle.py)
NO_NEED = -inf                                                       # need(): these two never conflict
def local_overrides_from_file(pcb_path) -> dict                      # footprint/pad clearance overrides + mask margins
    # {'mask_margin', 'mask_web', 'fp_clearance': {ref: mm}, 'pad_clearance': {(ref, num): mm},
    #  'fp_mask_margin': {ref: mm}, 'pad_mask_margin': {(ref, num): mm}}  — text read, stdlib, ~0.1 s on the largest board
def raster_guard(pitch: float, reach: float) -> float                # grid inflation that makes cell-to-cell steps legal (A1)
def dp_complement(name: str) -> str | None                           # KiCad's differential-pair suffix rule
def shape_gap(a: geom.Shape, b: geom.Shape) -> float                 # exact edge-to-edge distance of two shapes
class Oracle:
    def __init__(self, board, tol: float = TOL_MM, *, overrides: dict | None = None): ...
        # engine: Oracle(board, overrides=local_overrides_from_file(pr.pcb))
    def seg_ok(self, layer: int, x1, y1, x2, y2, w: float, net: str, clearance: float | None = None,
               ignore: frozenset[int] = frozenset()) -> Blocker | None      # None = legal; the WORST hard blocker
               # (order-independent); soft blockers not returned; clearance = plan override for this item (>= class)
    def via_ok(self, x, y, size, drill, net: str, ignore: frozenset[int] = frozenset(),
               clearance: float | None = None) -> Blocker | None            # through via, every layer
    def seg_legal(...same as seg_ok...) -> bool;  def via_legal(...same as via_ok...) -> bool   # first violation stops
    def piece_ok(self, p: Piece, ignore=frozenset(), clearance=None) -> Blocker | None          # p itself is ignored
    def soft_hits(self, layer, x1, y1, x2, y2, w, net) -> list[Blocker]      # thermal / reserve shapes of OTHER nets
               # whose area the copper overlaps; a via: x2 == x1, y2 == y1, w = size, once per layer
    def blockers(self, layer, x1, y1, x2, y2, w, net, limit: int = 8, clearance=None, ignore=frozenset()) -> list[Blocker]
               # all hard violations + soft hits, nearest first
    def via_blockers(self, x, y, size, drill, net, limit: int = 8, clearance=None, ignore=frozenset()) -> list[Blocker]
    def need(self, net: str, half_w: float, shape, layer: int, kind: str = "seg", drill: float = 0.0,
             clearance: float | None = None) -> float
               # required copper-edge gap to a shape from near(); NO_NEED when none; kind 'via' needs its drill
    def near(self, layer: int, bbox, grow: float) -> list            # geom.Shape: static (cls cu|hole|kt|kv), outline
               # segments (cls 'edge'), hardened soft shapes (cls 'soft'), then dynamic copper sorted by piece id
               # (shape.tag = piece id; a via gives a 'cu' and a 'hole' shape)
    def near_soft(self, layer: int, bbox, grow: float) -> list       # soft shapes; tag = (rule, soft id), net = owner id
    def add_piece(self, p: Piece) -> None;  def remove_piece(self, p: Piece) -> None;  def has_piece(self, pid) -> bool
    def add_soft(self, rule: str, net: str, layer: int, shape) -> int   # rule 'thermal'|'reserve'; layer -1 = all;
               # shape = geom.Shape | box 7-tuple | polygon point list
    def harden(self, soft_id: int) -> None                           # closed to foreign nets from now on (need 0)
    def remove_soft(self, soft_id: int) -> None
    def set_clearance(self, net: str, clearance: float) -> None      # plan override; applies to checks and later pieces
    def clearance(self, net: str) -> float;  def net_id(self, net) -> int;  def inside(self, x, y) -> bool
    def pad_gap(self, pad_a: str, pad_b: str, layer: int) -> float   # exact edge-to-edge gap, mm; inf if no copper there
    @property
    def version(self) -> int                                         # bumps on every add/remove (pieces and soft shapes)
    checks: int                                                      # calls so far (budget: 1 unit per 8)
    mask_openings_hard: bool = True; tol: float

# pieces.py  (as built by W1; tests/test_v2_pieces.py. Details: docs/internal/v2-interface-notes.md, W1 1-8)
TEE_TOL = 1.5e-6                                   # mm: "on the centre line" for connected()
def is_open(t: Terminal) -> bool                   # open terminal = "anything else of the net": no pad, no cluster, layers == ()
class RouteState:
    def __init__(self, oracle: Oracle, conns: list[ConnV2]): ...          # conns is held by reference
    def begin(self) -> int;  def commit(self, tok: int) -> None;  def rollback(self, tok: int) -> None;  depth: int
    def add(self, **piece_fields) -> Piece;  def remove(self, pid: int) -> None      # add: Piece fields without id; quantised; not checked
    def rip_conn(self, conn: int, *, protected: bool = False) -> list[Piece]         # protected pieces stay unless asked; status -> 'todo'
    def pieces(self, conn: int | None = None, net: str | None = None) -> list[Piece];  def piece(self, pid) -> Piece | None
    def set_status(self, conn: int, status: str, fail: dict | None = None) -> None   # 'todo'|'routed'|'failed'
    def status(self, conn: int) -> str;  def fail(self, conn: int) -> dict | None;  def conn(self, idx: int) -> ConnV2
    def connected(self, conn: int, loose: bool = False) -> bool       # loose: KiCad's rule (an end inside the other's copper)
    def net_groups(self, net: str, loose: bool = False) -> list[tuple[list[int], list[int]]]   # (cluster ids, piece ids)
    def dangling(self, net: str, loose: bool = False) -> list[int]    # 'routed' connections no longer connected
    def snapshot(self) -> "Snapshot";  def restore(self, snap: "Snapshot") -> None
    def score(self) -> Score;  new_errors: int                        # new_errors: journaled property
    def export(self, only=None) -> tuple[list, list, list[int]]       # worker tracks, vias, piece ids (track ids, then via ids)

# kio.py  (KiCad I/O; wraps session._run/_scratch/_raw_drc/_sig; no routing logic; as built by W1, tests/test_v2_kio.py)
def extract(pr, ripup: str = "none", nets: list[str] | None = None) -> dict      # + ex["_ripped"]; nothing is saved
def scratch(pr) -> Project;  def cleanup(sc) -> None;  def baseline_drc(pr, ripup="none", nets=None) -> list
def commit_and_check(pr, scratch, tracks: list, vias: list, baseline_raw: list, *, ripup: str = "none", rip_nets=None,
                     fresh: bool = True, remove: list[str] | None = None, drc: bool = True) -> dict
    # fresh: scratch.pcb is first reset to pr.pcb (then ripped up); fresh=False commits into it as it is on disk
    # -> {uuids: track uuids + via uuids, track_uuids, via_uuids, new: [marker...], raw: [...],
    #     unconnected: {net: n}, stats: {...}, ex: extract of the committed, refilled board}
def new_markers(raw: list, baseline_raw: list) -> list[dict]
    # marker = {type, message, items: [text, text], at: [x, y], uuids: [...], pos: [[x, y], ...], copper: bool}
def marker_pieces(marker: dict, uuids: list[str], piece_ids: list[int], state: RouteState, radius: float = 0.3) -> list[int]

# legacy.py  (stage-1 detail engine: v1 Router on a RouteState; as built by W1, tests/test_v2_legacy.py)
RIP_LEVEL = "conn";  EMIT_CHECK = True;  EMIT_RETRIES = 3;  SELF_CHECK = "internal clearance self-check failed ..."
def route_all(ctx: Ctx, budget: Budget, only: list[int] | None = None) -> StageReport
    # runs guided.router.Router (subclassed, v1 files untouched) on ctx.board with the pieces already in ctx.state as
    # fixed copper; every path is checked by the oracle when it is found (rejected: the violated shapes are
    # re-rasterised with raster_guard and the search runs again); negotiation and the final self-check rip
    # connections (plus what teed onto them), never nets; a connection failing the self-check is re-queued once.
    # Results: Pieces (stage='legacy'), status, state.fail(idx) / report.data['failed'] in v1's format.
def plan_connections(board, cfg, mode: str = "signals", start: int = 0) -> list[ConnV2]   # v1's plan; 'pours' | 'all'
def skipped(board, cfg) -> list[dict]                                                     # v1's skipped[]

# classify.py
def run(ctx: Ctx, budget: Budget) -> StageReport          # fills ctx.conns, ctx.x.auto
def roles(board, cfg, x) -> dict[str, str]
def detect_buses(board) -> list[dict]
def connections(board, cfg, x, roles: dict) -> list[ConnV2]

# power.py
# stage 4/5 integration, additive: pour_at_risk(ctx, pad) -> bool (S1 step 4b, data['risk_dogbones']);
#   place_dogbone(..., lanes=True); open_lanes / closed_lanes (DECAP_LANE_CHECK, off)
def run(ctx: Ctx, budget: Budget) -> StageReport
def dogbone_candidates(pad, via: tuple[float, float], clearance: float) -> list[tuple]   # (stub_end_xy, via_xy)
def place_dogbone(ctx: Ctx, pad, exclude: frozenset = frozenset()) -> list[Piece] | None
def thermal_spokes(board, pad) -> list                                                  # geom shapes

# tiles.py  (as built by W3; tests/test_v2_tiles.py)
CAL = 1.0; CAL_GATE = 0.995; CORRIDORS_ADVISORY = False                       # measured, Appendix A3
class Tiles:
    def __init__(self, board, oracle: Oracle, tile_mm: float, ref_w: float, ref_cl: float, cal: float = 1.0, *,
                 via: tuple[float, float] | None = None): ...                 # via = (size, drill), default class
    nx: int; ny: int; nl: int; t: float; G: int; N: int; x0: float; y0: float; pitch: float; version: int
    capE: list; capN: list; hardE: bytearray; hardN: bytearray                # flat [(layer*ny + ty)*nx + tx]
    advisory: bool; reservations_booked: bool; regions_scaled: bool
    def tile_of(self, x, y) -> tuple[int, int];  def center(self, tx, ty);  def rect(self, tx, ty)
    def cap(self, layer: int, tx: int, ty: int, side: str) -> float           # mm, side 'E' (to tx+1) | 'N' (to ty+1)
               # | 'W' | 'S';  = (raw_cap + slack) × cal × scale − booked;  0.0 off the grid
    def raw_cap(self, layer, tx, ty, side) -> float;  def tracks(...) -> int;  def slack(...) -> float
    def passable(self, layer, tx, ty, side) -> bool                           # False: wholly outside the outline
    def free_intervals(self, layer, tx, ty, side) -> list;  def blocked(...) -> list   # [(lo, hi, tag)]
    def pad_edges(self, pad: str | None) -> frozenset                         # border codes in the pad's own shadow
    def own_shadow(self, code: int, pos: float, net_id: int, half: float = 0.0) -> bool
    def edge_of(self, code: int) -> tuple[int, int, int, str]                 # code = index*2 + (0 E | 1 N)
    def via_cap(self, tx: int, ty: int) -> int
    def book(self, layer, tx, ty, side, mm: float) -> None;  def book_via(self, tx, ty, n: int = 1) -> None
    def book_shape(self, layer: int, shape, grow: float = 0.0) -> int         # a reserved area as demand
    def scale(self, layer, tx, ty, side, factor: float) -> None               # feedback / region.capacity_scale
    def scale_rect(self, rect, factor: float, layers=None) -> int
    def pad_cut(self, pad_a: str, pad_b: str, layer: int, w: float, cl: float) -> Cut
    def neighbour_cuts(self, ref: str, layer: int, w: float, cl: float, max_gap: float = 3.0) -> list[Cut]
    def cut_for_edge(self, layer, tx, ty, side, demand: list[tuple[str, float]]) -> Cut
    def set_usage(self, useE, useN, useV) -> None;  def utilisation(self, layer: int) -> list[list[int]]   # percent
def make(board, oracle, conns=None, tile_mm: float | None = None, cal: float | None = None) -> Tiles
def choose_tile_mm(board, max_nodes: int = 60_000) -> float;  def min_pad_pitch(board) -> float
def reference_class(board, conns=None) -> tuple[float, float];  def default_via(board) -> tuple[float, float]
def reference_demand(tiles: Tiles, tracks: list, own: bool = True, by: str = "count") -> dict   # calibration helper
def calibration(tiles: Tiles, tracks: list, cals=(0.8, ..., 1.3), by: str = "count") -> dict

# globalroute.py  (as built by W3; tests/test_v2_globalroute.py)
def run(ctx: Ctx, budget: Budget, only: list[int] | None = None) -> StageReport   # fills ctx.corridors, ctx.tiles,
               # ctx.history['global'] {path, layer, tee, crossings, cuts, ...}; only = partial re-route (feedback)
def pathfinder(tiles: Tiles, conns: list[ConnV2], cfg, x, budget: Budget, *, dirs: dict | None = None,
               state: "GlobalState | None" = None) -> dict[int, list[tuple[int,int]]]
def assign_layers(tiles: Tiles, conn: ConnV2, path2d: list, cfg, x, *, state=None, end_layers=None) -> tuple
               # (layers, layer_seq);  layer_seq = ((i, from_layer, to_layer), ...): a via in tile path2d[i]
def layer_directions(board, conns: list[ConnV2], tiles: Tiles | None = None, layers=None) -> dict[int, str]
class GlobalState: ...                                                       # usage, history, 3-D tile paths

# window.py
class Window:
    grid: "guided.grid.Grid"; pitch: float; layers: tuple[int, ...]; box: tuple
    cm: "guided.grid.ClassMap"; owner: list          # owner[cell] = piece id | 0
    def cell(self, x, y, layer) -> int;  def xy(self, state: int) -> tuple[float, float, int]
    def block(self, cells) -> None
def choose_pitch(board, box, w: float, cl: float, layers: int) -> tuple[float, float, float]   # p, ox, oy
def build(ctx: Ctx, conn: ConnV2, tiles_mask: frozenset | None, box, *, width: float, pitch: float | None = None,
          soft: bool = False) -> Window

# search.py
def search(win: Window, net_id: int, sources: dict[int, float], targets: set[int], *, h: list[float] | None,
           turn, via_cost: float, layers_ok, cmul=None, hist=None, soft_cost=None, bend_state: bool = False,
           limit: int, budget: Budget) -> tuple[list[int] | None, dict]
    # info: {expanded, reason: 'ok'|'limit'|'budget'|'exhausted', crossed: {piece_id: cells}, frontier: [...]}
def heuristic(win: Window, targets: set[int], tile_dist: dict | None) -> list[float]
def probe(win: Window, net_id: int, sources: dict[int, float], limit: int, budget: Budget) -> dict
    # {enclosed: bool, reached: int, frontier_blockers: [(piece_id|tag, count)], static_share: float}

# detail.py
def run(ctx: Ctx, budget: Budget, only: list[int] | None = None) -> StageReport
def route_conn(ctx: Ctx, conn: ConnV2, budget: Budget, *, widen: int = 0, pitch_scale: float = 1.0,
               extra_vias: int = 0, soft_cost=None) -> dict
    # {ok, pieces: [Piece], fail: {reason, where, blocked_by, static_share, tried}, crossed: {conn: cost}}
def pattern(ctx: Ctx, conn: ConnV2) -> list[tuple] | None            # polyline or None
def emit(ctx: Ctx, conn: ConnV2, win: Window, path: list[int], width: float) -> list[dict] | Blocker

# flow.py
def min_cost_flow(n: int, edges: list[tuple[int, int, int, float]], s: int, t: int, want: int) -> tuple[int, float, list[int]]
    # edges (u, v, cap, cost) → (flow, cost, flow per edge); successive shortest paths, deterministic

# fanout.py
def run(ctx: Ctx, budget: Budget) -> StageReport
def needs_fanout(board, ref: str, w: float, cl: float) -> bool
def ring_fan(ctx: Ctx, ref: str, row: list, style: str) -> list[dict]        # per pad: pieces + terminal | fail
def via_escape(ctx: Ctx, ref: str, pads: list) -> list[dict]
def re_escape(ctx: Ctx, conn: ConnV2, end: str, avoid_side: str) -> bool     # used by repair rescue

# repair.py
def run(ctx: Ctx, budget: Budget, only: list[int] | None = None) -> StageReport
def mincut_rip(ctx: Ctx, conn: ConnV2, budget: Budget, tabu: set) -> bool
def permute_cluster(ctx: Ctx, conns: list[ConnV2], budget: Budget) -> bool
def rescue(ctx: Ctx, conn: ConnV2, budget: Budget) -> bool
def rip_price(ctx: Ctx, victim: int, piece: Piece | None = None) -> float   # piece: its protected flag; reservations
# as built (W7), additive: probe(ctx, conn, budget=None) -> dict; certificate(ctx, conn, budget=None) -> dict;
#   memory(ctx) -> Memory (ctx.history['repair']); DEPS (sibling overrides for tests); RESCUE_FIRST.
#   run(): StageReport.data = {repaired, failed, certificates, stats, log, tried, feedback_loops, tabu}

# polish.py
def run(ctx: Ctx, budget: Budget, passes: tuple[str, ...] = ("entry", "tight", "stairs", "vias", "widen")) -> StageReport
    # as built (W8): unknown pass -> ValueError; report.data = {edits: {pass: n}, edits_total, tried, vias_removed,
    # widened_mm, deferred, rejects: {why: n}, slack_mm, loose, oracle_checks, delta: {necked_mm, vias, length_mm,
    # bends}, score_before, score_after}; reads the optional ctx.x.polish_slack_mm (default 0)
def polylines(state: RouteState, conn: int) -> list[list[Piece]]
    # segments of one connection as runs in path order, broken at vias, junctions and open ends

# judge.py
def run(ctx: Ctx, budget: Budget, pr, scratch, baseline_raw: list, reroute: Callable[[list[int], Budget], None]) -> StageReport
def pours_pass(ctx: Ctx, pr, scratch, budget: Budget) -> StageReport

# diag.py  (as built by W9; details and what it reads from other stages: docs/internal/v2-interface-notes.md, W9 6-9)
def failure(ctx: Ctx, conn: ConnV2, fail: dict) -> dict                      # the failed[] entry: v1 keys + conn, cut,
    # static_share, mincut_nets, probe {enclosed: a|b|none}, tried (+ private _a, _b, _frontier); cached in
    # ctx.history['diag_failed'][conn.idx]; missing cut / static_share are derived (pad-pair gap, oracle probe)
def stages(ctx: Ctx) -> list[dict];  def auto(ctx: Ctx) -> dict
def congestion(ctx: Ctx) -> dict     # {tile_mm, cell_mm, origin, cols, rows, capacity: tiles|estimate, layers, idle_layers, max, hot}
def cuts(ctx: Ctx, top: int = 10) -> list[dict]      # {a, b, layer, capacity_mm, demand_mm, nets, overflow_mm, at}
def critique(ctx: Ctx, reference: dict | None = None) -> list[dict]          # reference = {net: [length_mm, vias]}
def suggest(ctx: Ctx, failed: list[dict], crit: list[dict]) -> list[dict]    # also leaves overlay_set() on ctx.x.overlays
def overlays(ctx: Ctx, kind: str, conn: int | None = None) -> list[dict]     # kind congestion|sketch|failure; rect / line /
    # text primitives in mm inside the board bbox (+ a 'view' rect to crop to for a failure)
def overlay_set(ctx: Ctx, failed: list[dict] | None = None) -> dict          # {congestion, sketch | None, failures: [...]}
def draw(img, to_px: Callable, prims: list[dict]) -> None                    # Pillow
def render(board, prims: list[dict], path, title: str = "", width: int = 1400) -> str     # PNG (crops to 'view')
def build(ctx: Ctx, fails: dict | None = None, reference=None, with_overlays=True) -> dict   # every §6.2 key at once

# planx.py  (as built by W9; V2Cfg fields: docs/internal/v2-interface-notes.md, W9 3-5)
# stage 4/5 integration, additive: V2Cfg.plane_rescue (plan.global.plane_rescue, default True);
#   v2_plan(suggested) -> dict  (engine = 'v2', v1's automatic ground `skip` dropped)
ENGINES = ("guided", "v2"); DEFAULT_ENGINE = "v2"; STAGES = (...)            # the default since 2026-11-11 (owner decision)
V2_GLOBAL: dict; V2_NET: dict; V2_TOP: dict; V2_REGION: dict                 # name -> help text
class V2Cfg: ...                                                             # resolved v2 hints + auto{}: engine, stages,
    # tile_mm, negotiation_iters, power_width_factor, fanout, polish, nets{name: NetX}, buses, fanout_parts, reserve,
    # regions[RegionX], given, warnings, auto, overlays; net(name), bus_of(net), fanout_for(ref),
    # capacity_scale(x, y, layer, net), no_vias(x, y, net), has(stage), last_stage, as_dict()
def extend_schema(plan_module) -> None                                       # idempotent; called at the bottom of plan.py
def resolve(board, plan: dict, cfg) -> V2Cfg                                 # raises BAD_PLAN via plan._bad; plan.resolve
    # calls it last and stores the result as cfg.x (plus cfg.engine, cfg.stages)
def apply_clearances(oracle, board, cfg) -> int;  def merge_patch(plan, patch) -> dict
def auto_preview(board, cfg) -> dict;  def pad_minor(pad) -> float

# engine.py
# stage 6/7 second pass, additive: VIA_FRUGAL_LADDER / _frugal_ladder() (COPPER_V2_FRUGAL_X), _Run.halos() (off:
#   HALO_MM, COPPER_V2_HALO), detail.AUTO_DIR_BIAS (off: COPPER_V2_AUTODIR), Oracle._hole_override.
# stage 6/7 integration, additive: _frugal(...) before the normal run (VIA_FRUGAL, VIA_FRUGAL_FAILS[_MIN];
#   plan.global.via_frugal, COPPER_V2_FRUGAL=0); pass 'vias' = polish.revia(ctx, budget) before polish (REVIA,
#   REVIA_SHARE, COPPER_V2_REVIA=0). detail: route_conn(..., via_limit, via_scale, rungs_max, lateral),
#   run(..., max_failed) -> data['hard_failed'], data['aborted']. planx: V2Cfg.via_frugal.
# stage 4/5 integration, additive: after the fallback and the ladder the engine runs plane_rescue() -> retree() ->
#   plane_ladder() (passes 'planes', 'retree', 'planes-ladder'); pour_connect() gives failed pad-to-pour
#   connections the window search. Switches: PLANE_COST, PLANE_SHARE, RETREE, RETREE_SHARE, PLANE_LADDER,
#   PLANE_LADDER_SHARE, POUR_WINDOW; COPPER_V2_PLANES / _RETREE / _PLANE_LADDER = 0
STAGES = ("classify", "power", "fanout", "global", "detail", "repair", "polish", "judge")
def route(pr, cfg, x: V2Cfg, ex: dict, *, scratch, baseline_raw, budget_units: int, progress=None) -> dict
    # returns the same dict shape session.route builds from v1 results (routed, total, percent, failed, skipped,
    # nets, added, drc, passes, plan_used, _board, _cfg, _scratch) plus the §6.2 keys.
def available_stages() -> dict[str, bool]            # which stage modules are importable (drives fallbacks)
```

Fallback rule that lets files land in any order: `engine.py` uses a stage only if its module imports and its
feature flag in `available_stages()` is on; when `detail` is absent it calls `legacy.route_all`; when `global` is
absent `detail` gets `tiles_mask=None` and a box corridor.

---

## 10. Unit-test plan (one `tests/test_v2_<module>.py` each, run as `python tests/test_v2_x.py`, scratch config dirs only)

| Module | Tests |
|---|---|
| types | `q()` idempotent; `Score` ordering; `Budget` charge/sub arithmetic, no clock |
| oracle | gap exactly at rule legal, 2 µm under illegal; pad-specific, NPTH, edge, mask-bridge, keep-out cases on synthetic boards; same-net tee legal; add/remove restores answers; **differential test vs kicad-cli on corpus references and seeded violations (zero disagreements)**; `one_air_max` (126.51, 81.42) regression case |
| pieces | nested rollback restores oracle `version`-independent answers; snapshot/restore equality; `connected` with tees and vias; `export` order stable |
| kio | commit returns uuids in order (scratch board); marker→piece blame by uuid and by proximity; new-error diff |
| legacy | one bad segment rips one connection, not the net (synthetic fault injection); output pieces equal v1 copper on a clean board |
| classify | role precedence; GNDA routed; bus detection positive/negative/reversed; Steiner order deterministic; width clamps |
| power | dog-bone candidates canonical; escape-lane penalty picks the non-blocking site; no legal site → reported; spokes generated for TH and SMD pads |
| tiles | capacity of an empty border = `floor((T+cl)/p)·p`; pad row reduces it exactly; via capacity through all layers; `pad_cut` formula incl. diagonal; calibration on references reported |
| globalroute | two nets through a 1-track gap: one detours; overflow reported when impossible; convergence keeps best iteration; layer DP minimises vias under `max_vias`; `no_vias` respected; same twice |
| window | pitch ladder choices for 0.4/0.5/0.65/1.27 mm pitch; origin puts pad centres on cells; corridor mask blocks outside; LRU hit ≡ rebuild |
| search | finds optimal path on small grids vs brute force; multi-target; bend_state on/off both legal; soft mode reports crossed pieces; limit/budget reasons; heap determinism |
| detail | pattern hit for clear L; emitted geometry all oracle-legal incl. pad joins; failing join re-searches; bounded wide cap honoured; neck result carries `target_w` |
| flow | known small networks; infeasible → partial flow; deterministic ties |
| fanout | QFN 0.4 mm at-rule escape legal; slot order = pad order (no crossings); over-capacity ring → via escape; boxed pad reported with `Cut` |
| repair | A blocks B: min-cut rips only A and both route; tabu terminates a ping-pong pair; permutation solves a 3-net order puzzle; protected dog-bone only moved with replacement; rollback on worse score |
| polish | staircase → one diagonal; via pair removed when single-layer legal; pad entry centred; widen splits a segment at the tight spot; never changes connectivity; never worsens `Score` |
| judge | fake DRC (stubbed `kio`) with one marker removes one connection and re-routes; keep-best returns round 1 when round 2 is worse; residual marker → reported removal; pours pass uses reservation |
| diag | certificate fields complete; `suggest` patches validate against the plan schema; overlays within board bbox |
| planx | every new field accepted; unknown field → `BAD_PLAN` with fix; defaults ≡ no plan |
| engine | stage-limited dry run stops before detail and writes nothing; same twice on two corpus boards; monotone budget on a synthetic congested board; result has every contract key |

Existing suites `tests/test_guided_route.py` and `tests/test_pcb.py` are run before and after each WP that touches
a file outside `v2/` (they were not run for this document).

---

## 11. Integration

- **Benchmark**: `bench/route/routers/v2.py` — `NAME`, `available()`, `route(project_dir, pcb, options)`; same as
  `guided.py` but deep-merges `{"global": {"engine": "v2"}}` and `options["plan"]`; also returns `stages`, `cuts`,
  `critique`. Usage: `run.py --router v2 [--board X] --runs 2`, stage subsets via
  `--opt plan='{"global":{"stages":[...]}}'`. Results go to `results/v2_<stage>` with `--name`.
- **`pcb_route_guided`**: unchanged signature; `plan.global.engine` selects v2; `dry_run`, `compare`, `ripup`,
  `nets`, `render` behave as today. `_findings` gains fixes derived from `cut`/`suggest`.
- **`pcb_route_plan`**: adds `auto` and, when `engine` is v2, the global-stage `cuts` (cheap).
- **`pcb_route`**: its guided mode inherits the engine through the plan; default stays `guided` until the final
  gate, then `default_plan` sets `engine: "v2"` and `"guided"` remains the escape hatch.
- Regression gate at every stage: the 6 currently clean boards stay complete and clean; no board below baseline.

---

## 12. Staged build order

Parallelism: after W0 freezes `types.py` and the `Oracle` interface, W1–W9 can all be written at once against the
interfaces in §9 with unit tests on synthetic boards. The **stages** below are integration milestones on the
benchmark, not a serial coding order.

| Stage | Integrates | Engine behaviour | Gate (see §1 table) |
|---|---|---|---|
| 0 | W0 | nothing routed; oracle differential test; `one_air_max` root cause traced; existing tests green | zero oracle disagreements |
| 1 | W1, W2 | `classify`(minimal: v1 `plan_connections`) → `legacy` → `judge` (non-destructive, keep-best) | **beats baseline**: > 81.8 %, no board lower, new DRC ≤ 5, `coldfire` monotone |
| 2 | W5, W8-min | + roles/widths/GNDA, dog-bone reservations, thermal spokes, polish passes entry+stairs on legacy output | pour-pad failures on `coldfire`/`stickhub`/`cm5_minima` down ≥ 50 %; odd-angle < 15 % |
| 3 | W3, W4 | + tiles/global/corridors, window detail replaces legacy as primary (legacy = fallback per connection) | ≥ 92 %; `interf_u` ≥ 90 %; no "never tried" connections at default budget |
| 4 | W6 | + fan-out | `tinytapeout` ≥ 85 %; `synth_qfp_fanout` 100 %; boxed-in failures < 5 |
| 5 | W7 | + repair ladder, global feedback | ≥ 96 %; ≥ 10 boards clean |
| 6 | W8 | + full polish | vias ≤ 1.3× human; odd-angle < 5 %; necked share reported |
| 7 | W9 | + plan fields, diagnostics, images, tool text; bus lateral ordering; default flip | final targets; steering demo: one plan iteration improves ≥ 1 failing board |

Why stage 1 must already win: it removes the two largest measured losses that need no new search (whole-net rip:
75 + 87 connections; destructive repair: 26 + 4 connections and the `coldfire` regression) while using v1's search
unchanged, so the 6 clean boards cannot change unless verify/judge fire, which they do not on clean boards. This is
an argument, not a measurement; the gate decides.

**Stage 1 as measured** (integration; `bench/route/results/v2_stage1.md`, rerun with
`python bench/route/run.py --router v2 --name v2_stage1 --opt stage1=true --runs 2`): 1880 / 2186 = **86.0 %**
(baseline 81.8 %), 0 new DRC errors (5), 6 / 14 boards complete and clean (the same six), no board below its
baseline, 14 / 14 identical twice. `coldfire` at 600 s (`--name v2_stage1_600 --opt stage1=true --opt
plan='{"global":{"time_budget_s":600}}' --board coldfire --runs 1`): 93.1 % = its default-budget result (the extra
budget buys a give-room attempt for 27 crowded pour pads, which the keep-best gate undoes). Wall time is below
baseline on every board above 10 s; `sonde_xilinx` (6.4 s vs 5.1 s) and `pic_programmer` (8.3 s vs 6.8 s) are over
the 1.2 x line by about a second: one more commit + DRC round trip than v1 (judge round, then pours round).
What stage 1 leaves: `tinytapeout` 43.2 % (183 connections never tried inside the budget, fine pitch) and
`interf_u` 79.9 % (20 never tried; 4 removed for starved thermals) are search limits for stages 3 – 5; 27 + 19 pour
pads on `coldfire` / `cm5_minima` are walled in by routed signals (stage 2: reservations before signals).

**Stages 2 + 3 as measured** (integration; `bench/route/results/v2_stage3.md`, rerun with
`python bench/route/run.py --router v2 --name v2_stage3 --runs 2`, serial): 2038 / 2186 = **93.2 %**, 0 new DRC
errors, 8 / 14 boards complete and clean, no board below its baseline, 14 / 14 identical twice, `interf_u` 90.9 %,
no connection left untried (22 end on "search effort limit" after a fair-share attempt), odd-angle ≤ 1.2 % per
board. `coldfire` at 600 s (`v2_stage3_600`): 92.5 % ≥ 91.4 % at default. As built the pipeline is window detail →
fair legacy fallback → repair ladder, plus a second opinion (build stage 1's pipeline from scratch, keep-best by
KiCad's verdict) on boards ≤ 250 connections that the staged pipeline does not finish: interface notes,
"Integration — build stages 2 + 3". Not met from the §1 table / stage 2 row: `cm5_minima` wall time 196 s (> 180 s);
segments < 0.2 mm above 10 % on `stickhub` 28.9, `cm5_minima` 24.4, `phone_breathalyzer` 14.3, `one_air_max` 13.7;
vias on common nets 2.2× human on `coldfire`; pour-pad failures fell on `coldfire` (27 → 4) but not on `stickhub`
(6 → 8) or `cm5_minima` (19 → 18), where the kept result is the second opinion; `coldfire` 91.4 % is below build
stage 1's 93.1 % (above its 90.2 % baseline); GNDA stays skipped while the suggested plan is v1's (it writes
`skip: true`; resolves with the default flip).

**Stages 6 + 7 as measured — final gate MISSED, default engine stays `guided`** (`bench/route/results/v2_stage7.md`,
rerun with `python bench/route/run.py --router v2 --name v2_stage7 --runs 2`, serial): 2113 / 2186 = **96.7 %**
(target 97 %), **10 / 14** complete and clean (target 11), 0 new DRC errors, no board below baseline, 14 / 14
identical twice, odd-angle <= 1.2 %. Vias on common nets: 1.05 x the human total over the corpus; per board
<= 1.25 x except `coldfire` 2.26 x, `phone_breathalyzer` 1.78 x (was 3.3 x), `sonde_xilinx` 1.67 x (5 vs 3).
Wall time: `cm5_minima` 181.5 / 183.1 s on a shared machine (target 180 s). Completion and the set of open
connections are those of stage 5: these two stages changed how easy boards are routed, not what the hard ones
reach. Added (interface notes, "Integration — build stages 6 + 7"): a **via-frugal run** — a board without plane
layers is first routed at 3 x the plan's via cost and that result is kept when its detail stage fails next to
nothing and it ends complete and clean, else the normal run starts fresh (`global.via_frugal`); a **via-frugal
re-route** before polish (`polish.revia`: a connection with vias is re-laid by the window search with one via
fewer allowed, on boards the search finished, poured nets excepted); an early undo in the pours pass
(`cm5_minima`: 13 -> 10 KiCad round trips). **Steering demo** (`bench/route/steer_demo.py coldfire --rule order
--base v2_stage7`): the `order` patches of run 1's `suggest[]`, merged into one plan revision, take `coldfire`
from 20 open to 14 (95.8 % -> 97.1 %), 0 new DRC, identical twice; the same rule makes `interf_u`,
`tinytapeout` and `cm5_minima` worse. Not built: bus lateral ordering; a polish pass against segments < 0.2 mm
(above 3 % on 9 boards). Why the gate is out of reach without a different plan: `coldfire`'s reference uses all
four layers for signals, the remaining open connections on `tinytapeout` are pads of a 0.4 mm QFN whose supply
pins close their neighbours' exits, and `interf_u` is a 2-layer board at capacity (stage 5 notes).

**Stages 6 + 7, second pass, as measured — final gate still MISSED, default engine stays `guided`**
(`bench/route/results/v2_stage7b.md`, rerun with `python bench/route/run.py --router v2 --name v2_stage7b --runs 2`,
serial): 2112 / 2186 = **96.6 %** (target 97 %), 0 new DRC errors, **10 / 14** complete and clean (target 11), no
board below baseline, 14 / 14 identical twice, odd-angle <= 1.2 %, wall time <= 146.5 s on every board (met:
`cm5_minima` 181 s -> 146 s). Vias on common nets over 1.25 x on `coldfire` (2.26 x), `cm5_minima` (1.90 x) and
`phone_breathalyzer` (1.78 x). Changed (interface notes, "stages 6 + 7, second pass"): the **oracle applies a
pad's clearance override to its hole** (KiCad's `hole_clearance (pad clearance ...)` next to an NPTH pad; the judge
no longer has to remove that copper: 6 KiCad round trips instead of 10 on `cm5_minima`), and the via-frugal run is a
**ladder** (3 x, then 8 x while the run stays complete and clean; fewest vias wins: `sonde_xilinx` 5 -> 2 vias).
Built, measured and left off: automatic layer directions as a price in the window search (`detail.AUTO_DIR_BIAS`)
and a cost halo round fanned-out parts (`engine.HALO_MM`) — each moves the hard boards by 3 - 7 connections in
both directions with no trend, which is also the finding: on a congested board the sequential pipeline's result is
one draw from a wide distribution. The steering demo on `coldfire` still holds (20 -> 14 open with one plan
revision from `suggest[]`).

**Stages 4 + 5, third pass, as measured** (`bench/route/results/v2_stage5c.md`, rerun with
`python bench/route/run.py --router v2 --name v2_stage5c --runs 2`, serial): 2113 / 2186 = **96.7 %**, 0 new DRC
errors, **10 / 14** boards complete and clean (the gate's 10; `stickhub` is the tenth), no board below its baseline,
14 / 14 identical twice, `tinytapeout` 91.2 %, `synth_qfp_fanout` 100 %, every open connection with a `failed[]`
record (73 open, 73 records, per-net counts equal to KiCad's). Every board but `stickhub` has the copper of
`v2_stage5b`. What changed (interface notes, "third pass"): in the second opinion a poured net whose pour joins
less than half of its pads on signal layers (a local fill: `stickhub` `+5V`, 10 of 35 pads) is planned and routed
with the signals instead of stub by stub in the pours pass, and the repair ladder got two rules for trees the v1
search laid (dependents of a rip are re-routed only if still apart after the victim is back; a re-route that fails
pad to pad is tried as a bridge between the two parts of its tree). Not met, as before: `synth_qfp_fanout` and
`stickhub` are complete through the second opinion (staged pipeline: 1 and 10 open); `cm5_minima` takes 183 s
(§1: 180 s); the §1 via and short-segment lines; one `tinytapeout` record names neither a blocker nor a cut.

**Stages 4 + 5, second pass, as measured** (`bench/route/results/v2_stage5b.md`, rerun with
`python bench/route/run.py --router v2 --name v2_stage5b --runs 2`, serial): 2111 / 2186 = **96.6 %**, 0 new DRC
errors, **9 / 14** boards complete and clean — the gate's 10 is **still missed** —, no board below its baseline,
14 / 14 identical twice, `tinytapeout` 91.2 %, `synth_qfp_fanout` 100 % (second opinion; the staged pipeline
leaves 1), every open connection has a `failed[]` record with blockers / cut / what was tried, and the number of
records equals KiCad's open count on every board. Added (interface notes, "Integration — build stages 4 + 5,
second pass"): a **bridge mode** of the window search for pour pads (from any copper of the pad's side, fill
islands included, to any copper of the rest of the net; a **stitch via** where two islands of the net overlap),
four plain pours rounds while each pays, a **rip phase** in which KiCad judges bridges that need routed
connections or earlier pour stubs moved, **orphan copper** counted as open and pruned, and exact `failed[]`
records for pour pads. Open: `tinytapeout` 31, `coldfire` 20, `interf_u` 14, `cm5_minima` 8, `stickhub` 2 (`+5V`
J3.1, GND C9.2: no legal bridge, and no bridge whose victims find another way). Not met from the §1 table:
boards clean (9 < 10); wall time on `cm5_minima` (188 s > 180 s); vias and short segments as before.

**Stages 4 + 5 as measured** (integration; `bench/route/results/v2_stage5.md`, rerun with
`python bench/route/run.py --router v2 --name v2_stage5 --runs 2`, serial): 2100 / 2186 = **96.1 %** (one
connection above the 96 % line), 0 new DRC errors, **9 / 14** boards complete and clean (`one_air_max` is new; the
gate asks for 10 and is **missed** on this criterion), no board below its baseline, 14 / 14 identical twice,
`tinytapeout` 90.9 % (gate 85 %), `synth_qfp_fanout` 100 %, every open connection has a `failed[]` record with a
reason. Wall time 3 - 141 s per board. `coldfire` at 600 s (`v2_stage5_600`, one run): 96.9 % >= 95.8 % at default. What closed the gap from 93.2 %
is not the ladder as designed (it accepts few rips: W7) but four things around it — interface notes,
"Integration — build stages 4 + 5": a **plane rescue** (connections with no way on the signal layers may cross a
plane layer at cost x 2.5, last, with the ladder repeated under the same rule; plan field
`global.plane_rescue`), **re-tree** (connections that all end on one unreachable pad are re-aimed at each
other's far ends), **dog-bones for pour pads at risk** in fine-pitch rows above a plane, and the **window search
as second chance in the pours pass**. This departs from §0 ("not built: routing through partly poured inner
layers by default") in so far as the rescue is on by default; it touches a plane only for connections that
failed everywhere else (10 - 14 connections per board on the three boards that have planes).
Open after this stage, by board (KiCad's count): `tinytapeout` 32 (19 boxed-in pads of the 0.4 mm QFN U6, whose
supply pins — net class clearance 0.23 mm at 0.4 mm pitch — leave their neighbours no legal straight exit once
routed), `coldfire` 20 (6 boxed-in + 5 walled-in MCU pads, 6 pour pads), `interf_u` 15 (2-layer, no plane to
rescue through), `cm5_minima` 11, `stickhub` 8 (all pour pads: both layers' pours are cut into islands by the
routing; the kept result is still the second opinion). Not met from the §1 table: boards complete and clean
(9 < 10); vias on common nets up to 2.3 x human (`coldfire` 405 vs 179: the fallback and the plane rescue are not
via-frugal and polish removes few); segments < 0.2 mm above 5 % on 8 boards (`stickhub` 28.9 %, `one_air_max`
15.2 %, `phone_breathalyzer` 14.3 %, `cm5_minima` 14.1 %); the result is not monotone in budget on every board
(150 s: `tinytapeout` 85.8 %, `interf_u` 87.2 %).

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Oracle disagrees with KiCad at the rule distance | W0 differential test and seeded-epsilon calibration (A2: 0 false-legal on 14 243 reference items and on the ladder; `TOL_MM` = 0.3 µm); S7 still catches the rest (zone fills, kept arcs, custom rules) |
| `one_air_max` verify root cause is something else (e.g. model error in zones) | traced in stage 0 (A1): it **was** something else — on-grid diagonal steps past pad corners (grid EPS smaller than the step's dip), then a whole-net rip; `legacy.py` rips per connection and judges with the oracle |
| `legacy.py` needs more than a `_verify` override (v1 internals unread) | W1 budgeted for subclassing `Router`; v1 file itself is not edited |
| Window build cost in pure Python | measured first in W4; LRU, then per-tile raster cache |
| Tile capacity optimistic near irregular pads | calibration gate; detail failures feed capacity decrements (≤ 2 loops) |
| Reservations wall in signals | escape-lane scoring; rippable with replacement at price × 8 |
| Role/bus misdetection | conservative rules, `auto{}` reported, plan veto |
| Through vias consume all layers on 4–6 layer boards | per-column via capacity; `via_budget`; DP |
| Pour connectivity only known after refill | pours pass after judge; reserved sites kept until then |
| Last few % need placement or plan hints | certificates + `suggest[]`; reported, not hidden |

---

## 14. Electrical constraints stage

Status (2026-11-11): **step A is built** (model, plan syntax, detection, measurement, reporting, benchmark boards:
§14.12 "Step A"; what differs from this text is recorded in `docs/internal/v2-interface-notes.md`, section "Electrical
constraints, step A", and in the two notes marked *[as built]* below). **Step B is built** (length tuning:
`v2/tune.py`, `judge.tune_pass`, the group order in `classify`; §14.12 "Step B"; what differs from this text is in
`docs/internal/v2-interface-notes.md`, section "Electrical constraints, step B", and in the *[as built, step B]* note under
§14.7; measured before / after per group: `bench/route/results/constraints_tune.md`). **Step C is built**
(coupled pairs with paired vias and skew bumps: `v2/pairs.py`; per-layer width rules; keep-short order and the
plan's `max_length` contract: §14.12 "Step C"; what differs from this text is in `docs/internal/v2-interface-notes.md`,
section "Electrical constraints, step C", and in the *[as built, step C]* note under §14.8; measured:
`bench/route/results/constraints_final.md`). **Fix round 1 is built** (2026-11-12, after an independent
verification of step C found six failures: members boxed in by their neighbours now take a stretch on another
layer through two vias; an unreachable skew limit keeps the bumps that fit; per-layer widths reach the pad
escapes; one result tells one story about a pair; measuring the same copper again gives the same figures; what
differs from this text is in `docs/internal/v2-interface-notes.md`, section "Electrical constraints, fix round 1", and in
the *[as built, fix round 1]* note under §14.7; measured: `bench/route/results/constraints_fix1.md`). **Fix
round 2 is built** (2026-11-12, after the second verification: one extract now gives every `model.Board` the same
connectivity clusters, which ends an input-order-dependent open connection on boards with existing copper and
the lost `constraints` block of a call that only measures; a run that keeps none of its copper reports the board
as it is; a net KiCad shows open while the model has it routed is listed in `failed[]`; `docs/internal/v2-interface-notes.md`,
section "Electrical constraints, fix round 2"; measured: `bench/route/results/constraints_fix2.md`). The design
rests on a full read of
`v2/{types,planx,classify,detail,search,polish,judge,oracle,engine,kio,pieces}.py`, `guided/{plan,model,session}.py`,
`workers/guided_board.py` (`extract`), `tools/route_tools.py`, `mcp/catalogue.py` and `bench/route/*`. The KiCad
behaviour in §14.9 is **measured** (KiCad 10.0, `bench/route/cons_probe.py`, rerunnable); every completion,
coupling or wall-time number for the new code is a **target**, not a measurement.

Owner requirement (verbatim): "for the PCB, remember that certain traces need to be length matched, ect." Scope:
length-matched groups, differential pairs (coupled, with a skew limit), track-width / impedance classes, keep-short
nets. The server never calls a model: the plan carries the caller's heuristics, and what the plan leaves out is
detected deterministically from net names, KiCad net classes and the project's `.kicad_pro` / `.kicad_dru`.

### 14.0 Decisions in one page

| Question | Decision | Why |
|---|---|---|
| Where does the model live? | New `v2/constraints.py` (model, detection, measurement, report). `types.py` only gains the piece stage `"tune"`. `Score` is **not** changed. | `Score` is sliced positionally (`engine.stage` gate `'tidy'`: `k[:4]`); constraint quality is carried beside it in `ctx.history['cons']` |
| What is "length"? | Exactly what KiCad's DRC reports (§14.5, §14.9): all track copper of the net, minus the in-pad shortcut, plus via height between the layers used, plus pad-to-die | KiCad is the judge; a second definition would tune to the wrong number |
| Group target | The longest member after routing (KiCad's skew semantics: every net against the longest the rule matches), or an explicit `len` | measured: `skew_out_of_range ... target net length ... (from <longest>)` |
| When is length tuned? | A judged pass **after** `judge.run` and `judge.pours_pass` (`judge.tune_pass`), keep-best against the board accepted before it | completion first; tuning can then never cost a connection or add a DRC error; the "longest member" is only known when everything is routed; zero cost on boards without length groups |
| Pair routing | One object: centre line searched by the existing ladder at width `2w + gap`, offset to two lanes, fan-in only at pads, both nets added in one transaction; falls back to today's sequential routing | reuses pattern / window / corridor search unchanged; a pair that cannot be coupled still routes |
| Name-only length groups | **Reported, not tuned** (`mode: "report"`) with the patch that adopts them; only plan- or rule-backed groups get copper | a guessed group must not put meanders on a board nobody asked for (`synth_bus_cross` is a 24-net auto bus) |
| KiCad's constraint markers | `length_out_of_range`, `skew_out_of_range`, `diff_pair_gap_out_of_range`, `diff_pair_uncoupled_length_too_long`, `too_many_vias` never cause a rip-up; they are counted and reported per group | today two of them are in `session.COPPER_TYPES`, so the judge would *remove* a net for being too short |
| Impedance | The router enforces the **width** (per layer) that the plan or the KiCad net class gives and never necks such a net; ohms are a label with a closed-form estimate for the report | no field solver in stdlib worth trusting for copper; the class width is the engineer's (or the fab's) number |
| Off switch | `global.constraints: "auto" \| "plan" \| "report" \| "off"` | `report` lays exactly today's copper and only measures; `off` is today's router |

### 14.1 What exists today (read from the code), and what constraints need changed

- `guided/plan.py`: `diff_pairs: [{p, n, gap, width}]`, `find_pairs()` (suffix table `_PAIR_SUFFIX`), per-net
  `keep_short`, `max_length`, `max_vias`, `width`; `default_plan()` writes the name-detected pairs and
  `keep_short + max_vias: 0` for crystals. `NetCfg.dp_partner / dp_role / dp_gap` carry the pair.
- `classify.py`: role `pair` (the N net takes its P net's rank, `neck == width`), role `critical` for `keep_short`,
  `ROLE_ORDER = critical 0, pair 1, bus 2, signal 3`. `detect_buses()` is geometric (≥ 4 two-pad nets between two
  footprints) and says nothing about length.
- `oracle.py`: P and N of a pair **by KiCad's name rule** (`dp_complement`: `+ - P N` then digits / underscores) may
  approach to the class's `dp_gap`. `/USB_DP` + `/USB_DM` is *not* such a pair (KiCad would look for `/USB_DN`): its
  gap is the ordinary clearance, and no `inDiffPair` rule of KiCad's can address it.
- `detail.py`: a pair is two independent connections, N right after P ("back-to-back, not gap-controlled",
  `session._pair_report`). `polish.py` and `polish.revia` skip `role == "pair"`.
- `max_length` is validated but **not enforced** by v2 (only `diag.suggest` reads it). `Piece.length` is 0 for a
  via. Nothing measures a net the way KiCad does.
- `judge._blame`: every new marker whose type is in `session.COPPER_TYPES` is blamed on a connection, which is
  ripped and re-routed, then removed. `length_out_of_range` and `diff_pair_gap_out_of_range` are in that set.
- `model.dru_minimums()` reads only `(min …)` of clearance-type constraints; conditions are ignored.
- The worker's extract carries per net `class, clearance, width, via, drill, dp_gap, dp_width` (effective net
  class) and per track / via a `locked` flag that `model.Board` drops (`kept_tracks` has no lock bit). There is no
  stackup, no pad-to-die length and no `diff_pair_via_gap` in it.

### 14.2 Constraint model (`v2/constraints.py`)

Frozen dataclasses, plain data, built once per run (`engine._Run.setup`) and stored as `ctx.x.cons`
(`planx.V2Cfg.cons`, `None` until resolved). Lengths in mm.

```python
KICAD_TYPES = ("length_out_of_range", "skew_out_of_range", "diff_pair_gap_out_of_range",
               "diff_pair_uncoupled_length_too_long", "too_many_vias")
SOURCES = ("plan", "dru", "class", "name")          # precedence, strongest first

@dataclass(frozen=True)
class LenGroup:
    name: str                    # plan name | KiCad rule name | "G1", "G2" (canonical order)
    nets: tuple[str, ...]        # sorted; only nets with >= 2 pads
    tol: float                   # allowed spread: every member within [target - tol, target]
    target: float | None         # explicit length (plan `len`, or the rule's opt); None = the longest member
    lo: float | None             # absolute window of a KiCad `length (min ..)(max ..)` rule
    hi: float | None
    amp: float | None            # largest meander amplitude (plan `amp`); None = project / default
    mode: str                    # "tune" | "report"
    src: str; conf: float; why: str      # e.g. ("dru", 1.0, "rule 'sd_match': skew max 0.5 mm")

@dataclass(frozen=True)
class Pair:
    p: str; n: str
    width: float; gap: float     # copper edge to copper edge
    via_gap: float               # between the two via pads of a layer change
    skew: float | None           # max |len(p) - len(n)|; None = measured and reported only
    uncoupled: float | None      # max length not coupled, per net (KiCad's count)
    gap_lo: float | None; gap_hi: float | None       # KiCad diff_pair_gap rule window, when one exists
    coupled: bool                # True = route as one object; False = sequential (today's behaviour)
    kicad: bool                  # KiCad sees the names as a pair (oracle.dp_complement): its pair rules apply
    z: float | None              # differential impedance label, ohm (report only)
    src: str; conf: float; why: str

@dataclass(frozen=True)
class WidthRule:
    net: str
    by_layer: tuple[tuple[int, float], ...]     # ((layer index, mm), ...); () = one width on every layer
    width: float                 # the width where by_layer says nothing; the search width is max(all)
    neck: bool                   # False for every net with a z / per-layer width: never thinner than its rule
    z: float | None
    src: str; why: str

@dataclass(frozen=True)
class Short:
    net: str
    max_len: float | None        # hard when the plan's max_length or a KiCad `length (max ..)` rule gives one
    hard: bool
    src: str; why: str

@dataclass(frozen=True)
class Constraints:
    mode: str                                    # "auto" | "plan" | "report" | "off"
    groups: tuple[LenGroup, ...]; pairs: tuple[Pair, ...]
    widths: dict; short: dict; max_vias: dict    # net -> WidthRule | Short | int (KiCad via_count rule)
    unread: tuple[str, ...]                      # KiCad rules with a constraint of ours and a condition we cannot read
    notes: tuple[str, ...]
    def group_of(self, net) -> LenGroup | None
    def pair_of(self, net) -> Pair | None
    def width_on(self, net, layer) -> float | None
    def tuned(self) -> tuple[LenGroup, ...]      # mode == "tune" and Constraints.mode in ("auto", "plan")
    def any(self) -> bool
```

How length is counted is part of the model (it is what `tol` refers to): see §14.5. A net is in at most one
`LenGroup`; a pair's two nets may both be members of a group (then the group is matched net by net and the pair's
`skew` is checked separately). Multi-pad nets are legal members and are measured as KiCad measures them — the
**total** copper of the net, tee stubs included; the report flags them `branched` (per-branch matching, e.g. a
clock fanned out to several loads, is not in this build: §14.13).

### 14.3 Plan syntax (`planx.py`) — small on purpose

Everything optional; unknown keys raise `BAD_PLAN` with the nearest valid name, as for every other field.

```
global:      {constraints: "auto" | "plan" | "report" | "off"}          // default "auto"
groups:      [{nets: ["/SD_D?", "/SD_CLK"], tol: 0.5, len: 42.0, amp: 2.0, name: "sd", off: true}]
diff_pairs:  [{p, n, gap, width,  skew: 0.15, uncoupled: 5, coupled: false, z: 90}]
nets:        {"<name|glob>": {widths: {"F.Cu": 0.15, "In2.Cu": 0.11}, z: 50}}
// already there and reused: nets[..].width, max_length (hard), keep_short, max_vias
```

| Field | Rule | Meaning |
|---|---|---|
| `global.constraints` | one of the four | `auto`: plan + detection, tune / couple; `plan`: only what the plan declares (detection is still *listed* in `auto.constraints`); `report`: resolve and measure everything, lay exactly the copper of `off`; `off`: nothing resolved, v2 as before §14 |
| `groups[].nets` | required, names / globs, ≥ 1 routable net after matching (1 only with `len`); a net in two groups is `BAD_PLAN` | the members |
| `groups[].tol` | required unless `off`; 0.01 … 100 mm | allowed spread (see `LenGroup.tol`) |
| `groups[].len` | 0.1 … 1e5 mm | explicit target: every member in `[len − tol, len + tol]`; omitted = the longest member |
| `groups[].amp` | 0.3 … 20 mm | largest meander amplitude for this group |
| `groups[].name` | string | label in reports (default `G<n>`) |
| `groups[].off` | `true` | veto: these nets are in no length group, whatever detection says |
| `groups: []` | empty list | no length groups at all (same convention as `diff_pairs: []`) |
| `diff_pairs[].skew` | 0.005 … 100 mm | max intra-pair length difference (tuned on the shorter net) |
| `diff_pairs[].uncoupled` | 0 … 1e4 mm | max uncoupled length per net |
| `diff_pairs[].coupled` | bool, default `true` when both ends allow it | `false` = route sequentially as before §14 |
| `diff_pairs[].z`, `nets[..].z` | 10 … 300 ohm | label; reported with an estimate, never changes a width |
| `nets[..].widths` | `{layer: mm}`, each ≥ the board minimum, layers must exist | per-layer width; `width` stays the value for unlisted layers |

Implementation points: `planx.V2_GLOBAL["constraints"]`, `planx.V2_TOP["groups"] = [GROUP_FIELDS]`,
`planx.V2_NET["widths" | "z"]`; `extend_schema()` also does `plan_module.PAIR_FIELDS.setdefault(k, …)` for
`skew, uncoupled, coupled, z` (the v1 table is the validator of `diff_pairs`), and appends one line to `SCHEMA_V2`
(≤ 260 B: it is part of `pcb_route_plan`'s result). `planx.resolve()` validates and stores the raw declarations in
`V2Cfg.cons_plan = {"mode", "groups": [...], "pairs": {p_net: {...}}, "widths": {...}, "z": {...}}`, adds
`constraints` to `as_dict()`, `groups` to `merge_patch`'s `_APPEND`, and records the fields in `x.given`. Under
`global.engine: "guided"` the fields are validated and flagged like every other v2 field. Documentation goes to
`registry.add_help("pcb_route_guided", …)` and this section — **no tool description or schema grows** (the
catalogue budget has ~6 B left).

Token cost of the common cases: a matched bus is `{"groups":[{"nets":["/SD_*"],"tol":0.5}]}` (38 B); a pair with a
skew limit adds `,"skew":0.15` to the entry `pcb_route_plan` already suggests.

### 14.4 Detection when the plan is silent

`constraints.read_project(pr) -> ProjectRules` (stdlib `json` + a small s-expression reader for the `.kicad_dru`,
which is the only place the project directory is needed: called from `engine._Run.setup` and from
`route_tools._auto`) and `constraints.resolve(board, cfg, x, proj) -> Constraints`. Precedence per net:
**plan > dru > class > name**. A weaker source never overrides a number of a stronger one; it may fill a field the
stronger one left empty (a plan pair without `skew` takes the rule's). Everything detected is listed in
`auto.constraints` with `src`, `conf` and `why`, in `pcb_route_plan` and in the route result.

1. **`.kicad_dru`** (authoritative for what KiCad will check). Read per `(rule NAME …)`: constraints `length
   (min/opt/max)`, `skew (max)`, `diff_pair_gap (min/opt/max)`, `diff_pair_uncoupled (max)`, `via_count (max)`,
   `track_width (min/opt/max)`. Condition subset understood (anything else → the rule name goes to
   `Constraints.unread`, a `RULE_NOT_READ` finding says KiCad still checks it):
   `A.NetName == '<name or * ? glob>'`, `A.NetClass == '<class>'`, `A.hasNetclass('<class>')`,
   `A.inDiffPair('<base glob>')`, joined by `||`; `&& A.Type == …` and a `(layer …)` clause restrict the layer /
   item type and are kept as such. Mapping: `skew` over ≥ 2 single nets → `LenGroup(tol = max, target = None)`;
   `skew` whose condition is `inDiffPair` → `Pair.skew`; `length` → `LenGroup(lo, hi, target = opt)` per matched
   net (one group per rule when it has both `min` and `max`); `length (max)` alone → `Short(max_len, hard)`;
   pair constraints → `Pair.gap_lo / gap_hi / gap (opt) / uncoupled`; `via_count` → `max_vias`; `track_width opt`
   → `WidthRule`. `src = "dru"`, `conf = 1.0`. A rule whose severity is `ignore` in the project's
   `rule_severities` is listed but not enforced.
2. **`.kicad_pro`**: net-class `diff_pair_width / diff_pair_gap` already arrive through the worker
   (`Net.dp_width / dp_gap`); read in addition `diff_pair_via_gap`, `board.design_settings.rules.
   use_height_for_length_calcs`, `tuning_pattern_settings.single_track_defaults / diff_pair_skew_defaults`
   (`min_amplitude`, `max_amplitude`, `spacing`: the tuner's defaults) and whether `tuning_profiles` is non-empty
   (KiCad 10 time-domain tuning: **not read**, named in `notes`). A class whose pair dimensions differ from the
   Default class, or whose name reads as an impedance (`90ohm`, `100R`, `Z50`), raises the confidence of the name
   pairs in it and gives them `z`. `src = "class"`.
3. **Names** (`src = "name"`):
   - Pairs: `plan.find_pairs()` stays the detector (`_P/_N`, `+/-`, `P/N`, `DP/DM`, `DP/DN`, `D+/D-`, `_H/_L`),
     then a geometric check sets `conf`: same pad count on both nets and every footprint that has a P pad has an N
     pad next to it (centre distance ≤ 3 × that footprint's pad pitch) → 0.9; same footprints, pads apart → 0.6;
     different footprints → 0.3. `coupled = conf ≥ 0.5`. Below that the pair is routed sequentially, exactly as
     today, and reported. Ground / power kinds never pair.
   - Length groups: a table `NAME_GROUPS = [(kind, member pattern, clock pattern, suggested tol)]` keyed on the
     common stem: SD / SDIO / eMMC (`D[0-7]|DAT[0-7]`, `CLK`, `CMD`), QSPI / OSPI (`IO[0-7]|DQ[0-7]`,
     `CLK|SCK`), RGMII / RMII (`TXD[0-3]`, `TX_CTL|TXEN`, `TXC|TX_CLK`; RX likewise), DDR byte lanes (`DQ\d+`,
     `DQS`, `DM`) and address / command (`A\d+`, `BA\d`, `CK`), ULPI, parallel video (`D\d+`, `PCLK`), and the
     generic "≥ 4 numbered members + one clock on the same stem" (`conf` 0.4). These groups get
     `mode = "report"`: measured, spread reported, and a `CONSTRAINT_GUESSED` finding carries the patch
     `{"groups": [{"nets": [...], "tol": <suggested>}]}`. They are **never tuned** on their own. The suggested
     tolerances are typical values, labelled as such; they are not taken from any interface standard here.
   - Keep-short: crystals (as today), plus switch nodes (`SW|LX|PH\d?` on a net with a pad of an `L*` part) and
     antenna / RF feeds (`ANT`, `RF`) → `Short(max_len = None, hard = False)`.

False positives stay cheap by construction: a wrongly detected pair is either not coupled (`conf < 0.5`) or coupled
only if both ends pass the gather test and every piece passes the oracle (§14.8) — otherwise it is routed
sequentially as today; a wrongly detected length group is a line in the report. Veto: `diff_pairs: []`,
`groups: []`, `{off: true}`, `coupled: false`, `global.constraints: "plan"`.

### 14.5 Length measurement — equal to KiCad's by construction

`constraints.net_length(ctx, net) -> LenParts(track, inpad, via, die, total, vias, branched)`:

```
total = Σ length of every track segment of the net (state pieces + board.kept_tracks: tee stubs count)
      − Σ over chains that END ON A PAD'S POSITION: (path length inside that pad − straight line pad centre → exit point)
      + Σ over vias: stack height between the top-most and bottom-most layer on which the net has a track end
                     or a pad at the via (0 when use_height_for_length_calcs is false or only one layer is used)
      + Σ pad-to-die length of the net's pads
```

- **Pad entry**: only a chain whose end point *is* the pad's position gets the shortcut (v2 always ends on pad
  centres: `Terminal.x/y`, polish pass `entry`). A track that ends elsewhere inside the pad, on its edge, or runs
  through the centre is counted in full and nothing is added. The exit point is where the chain first leaves the
  pad's copper on that layer (`pad.shapes[layer]`, exact shapes). Size of the effect: 0.05 mm per end for a
  dogleg inside a 1.2 × 0.6 mm pad; tenths of a millimetre on connector pads. A lane lying exactly on a pad edge
  is a numerical toss-up in KiCad (§14.9): the tuner keeps `END_KEEP` away from pads and the judged pass corrects
  against KiCad's own figure (§14.6 step 5).
- **Via height** (`constraints.Stackup`, read from `(setup (stackup …))` of the `.kicad_pcb` text like
  `oracle.local_overrides_from_file`; without one, KiCad's default: copper 0.035, mask 0.01 per side, the rest of
  `(general (thickness …))` split evenly over the dielectrics): sum of everything strictly between the two
  layers, plus the **full** thickness of an end layer that is an outer layer and **half** of one that is an inner
  layer. Checked against three measured hops (§14.9); inner-to-inner is inferred, not measured.
- **Pad-to-die**: `(die_length …)` of the pad, from the board file text (`constraints.die_lengths(pcb)`).
- Existing arcs arrive as 10° chords in the extract: a net with kept arcs is flagged `approx` and its figure is
  replaced by KiCad's whenever KiCad reports one.

`constraints.measure(ctx, kicad=None) -> dict` builds the whole report (§14.10) from `net_length`, `pairs.
coupled_stats` and, when a judged check is at hand, `constraints.kicad_markers(raw, targets)`, which parses the
five marker types (§14.9 formats; the net from the item text `[<net>]`). Parsing is a cross-check: when a message
does not match (another KiCad language or version) the type and the nets still count, the numbers are the router's.

### 14.6 Where it enters the pipeline

```
setup     constraints.read_project → constraints.resolve → ctx.x.cons, auto.constraints      (A)
S0        classify: members of a tuned group get priority max(own, 70); pairs / critical as today (A)
          widths: WidthRule → ConnV2.width = max over layers, neck = width (C)
S1–S3     unchanged
S4        detail.run: a coupled pair's P connection → pairs.route_pair (N is routed with it); else route_conn (C)
          emit(): piece width = cons.width_on(net, layer) (C)
S5        repair: nets of a coupled pair are plan-protected (repair._net_protected) (C)
S6        polish / revia: skip role pair (as today) and every connection that carries a "tune" piece (B)
          constraints.enforce_max_length(ctx): a net over a hard max loses its longest-detour connection,
          failed[] says by how much (C)
S7        judge.run → judge.pours_pass                (constraint markers are split off, never blamed) (A)
S7t       judge.tune_pass(ctx, pr, scratch, budget, tune=tune.run)     only when cons.tuned() or a pair has skew (B)
result    out["constraints"] = constraints.measure(ctx, kicad=last check); findings; compact form (A)
```

1. **Order.** Constrained nets go first in the sense the rank already expresses: `critical` (keep-short) 0, `pair`
   1; members of a *tuned* length group get `priority = max(priority, 70)` (clock 85 and high-speed 80 stay
   above) so they take direct paths and need less added length. Nothing changes when there are no constraints.
2. **Why tuning is not "S6c before the judge".** Before the judge, a meander can be blamed by a DRC marker and the
   judge's only remedy is to rip the whole connection and re-route it — untuned — inside its own rounds; the
   pours pass then needs the very room the meanders took. After the pours pass, the board KiCad accepted is a
   fixed point to fall back to, so the pass has a simple guarantee: **tuning never increases new DRC errors and
   never opens a connection**, or it is undone net by net. Cost: one extra commit + DRC round trip on boards that
   have something to tune (up to `TUNE_ROUNDS = 3`), none elsewhere.
3. **`judge._check`** (A): after `kio.commit_and_check`, `chk["cons"] = constraints.kicad_markers(res["raw"],
   targets)` and every marker of `KICAD_TYPES` is taken out of `chk["new"]` before the score and before
   `_blame`. `Score.new_errors` therefore counts copper errors only; the constraint errors are reported in
   `drc.constraint_errors` and per group. They are read from the *raw* list, not from the baseline difference: a
   `length (min …)` rule already fails on the unrouted board with "actual 0" and changes its text once routed.
4. **`judge.tune_pass`** (B), modelled on `pours_pass`:
   ```
   acc = _Accepted(ctx, scratch, j["last"])                 # the board as judged so far
   cal = {}                                                 # net -> KiCad length − router length, from markers
   for rnd in range(TUNE_ROUNDS):
       rep = tune(budget, only, cal)                        # adds stage="tune" pieces; returns per-net records
       if not rep.data["changed"]: break
       chk = _check(ctx, pr, scratch, baseline_raw)         # whole state from the base file, refill, DRC
       if len(chk["new"]) > acc.new or opens(chk) > opens(acc):
           nets = nets of the "tune" pieces kio.marker_pieces() names for the new markers
           if not nets: acc.restore(); break                # cannot attribute: back to the accepted board
           tune.untune(ctx, nets, avoid=marker rectangles)  # originals back, those places closed for the tuner
           only = nets; continue
       acc = _Accepted(ctx, scratch, chk)                   # at least as good: keep
       miss = nets KiCad still flags (chk["cons"]) although the router's figure says they fit
       if not miss: break
       cal.update(KiCad actual − router figure for miss); only = miss      # close the loop on KiCad's number
   if state is not acc's: acc.restore(ctx, scratch)
   ```
   Budget `total.sub(TUNE_SHARE = 0.05, floor=…)`; `self.passes.append({"pass": "tune", …})`.
5. **Existing and locked copper.** The tuner edits only `RouteState` pieces of this run. `board.kept_tracks`
   (everything that was on the board, locked or not) is measured and never altered. With `ripup: "nets" | "all"`
   the old copper of those nets is gone before routing and what replaces it is this run's, so it is tunable;
   locked tracks survive the rip-up (worker `op_ripup`) and stay untouched.
6. **Keep-best elsewhere.** `engine._frugal`'s candidate key becomes `(constraint misses, vias, length, mult)` and
   `engine._key(out)` gains the miss count as a third element, so a rung or a second opinion is not preferred
   merely for carrying fewer meanders. `session._better` (compare) is unchanged; the result notes how many mm of
   the length are meanders.

### 14.7 Length tuning (`v2/tune.py`) — engineer B

```python
STAGE = "tune"
def run(ctx, budget, only: set[str] | None = None, cal: dict | None = None) -> StageReport
def untune(ctx, nets, avoid: list | None = None) -> int          # restore the pre-tune pieces of these nets
def capacity(ctx, net) -> float                                  # mm the free runs of the net could add (report)
def gain(a: float, c: float) -> float                            # 2a − 4c(2 − √2): one chamfered U bump
```

**Shape** (all segments axial or 45° relative to the run, so the octilinear style holds on diagonal runs too):
a *U bump* leaves the run through a 45° chamfer `c`, runs perpendicular for the amplitude `a`, crosses a top of
length `F`, and comes back the same way; it occupies `F + 2c` of the run and adds exactly `2a − 4c(2 − √2)` mm
(checked against KiCad to 0.1 µm, §14.9). A *shallow bump* (diagonal up `h`, flat, diagonal down) adds
`2h(√2 − 1)`. Parameters:

| | Value |
|---|---|
| leg spacing `S` (centre to centre of parallel legs, also `F`) | `max(project spacing or 0.6, 3w, w + clearance)` |
| chamfer `c` | `clamp((S − 0.2) / 2, 0.15, 0.5)` → every straight piece ≥ 0.2 mm |
| amplitude `a` | from `a_min = 2c + 0.2` up to `a_max = groups[].amp`, else the project's `max_amplitude`, else 1.0 mm |
| shallow height `h` | 0.08 … `a_min` (gain 0.066 mm upward: tolerances below ~0.07 mm cannot be promised) |
| keep-off | `END_KEEP = max(2w, c + 0.2)` from the ends of the run; bumps on one side keep `S` apart |

**Algorithm**, per group in canonical order (`name`), per net in name order:

1. `L = net_length(net) + cal.get(net, 0)` for every member that is completely routed (a member with an open
   connection is reported `open` and takes no part). Target `T = target or max(L)`; window `[T − tol, T]`
   (`[len − tol, len + tol]` with an explicit `len`). Aim `T − min(tol / 4, 0.05)` (`len` exactly), accept inside
   the window shrunk by a 2 µm guard. A member above the window cannot be fixed by adding copper: reported
   "long by x mm" with the hint to raise `len` or route it earlier / shorter.
2. *Survey.* Candidate runs = straight state segments of the net that are not `protected`, not necked
   (`w == target_w`), not teed onto (polish's `_NetIndex`), outside the `avoid` rectangles of earlier rejections,
   longest first (`−length, layer, x1, y1, id`). Along each run, slots every `F + S`; for each slot and each
   side the largest amplitude on the ladder `a_max, ¾, ½, … a_min` whose ≤ 7 segments are all
   `oracle.seg_legal(layer, …, w, net, clearance)` (this includes the plan's forbid / avoid / strict-corridor
   rules: `plan_on`), cross no soft shape the run did not cross (`oracle.soft_hits`: thermal spokes, reserved
   sites), and keep `S − w` to every **other copper of the same net** (the oracle has no same-net rule: own
   pads, own vias, earlier bumps; `oracle.near` + `Shape.dist_seg`). The better side wins; ties: left.
3. *Allocation.* Take slots in survey order until their gains cover the need `E = aim − L`; lower the last
   amplitude so the sum is exact; if that falls below `a_min`, lower the previous one with it; `E` below one
   full bump → one or two shallow bumps. If the capacity is short, every slot is used at its largest amplitude
   (the best effort is kept when it lowers the group's worst deviation) and the record says
   `short by E − capacity`.
4. *Placement.* Per run one `RouteState` transaction: remove the run piece, add the new polyline
   (`stage="tune"`, same `conn`, `w`, `target_w = w`); keep it only if `state.connected` still holds for the
   connection and for every same-net connection that touched it, else roll back and drop that run. The removed
   pieces are remembered in `ctx.history["tune"]["orig"][conn]` for `untune`.
5. Deterministic: no clock, canonical orders above, coordinates through `q()`. Budget: `CHARGE_TRY` per oracle
   candidate, `CHARGE_EDIT` per transaction (polish's scale).

*[as built, step B]* Built as above with these differences (details and measurements:
`docs/internal/v2-interface-notes.md`, "Electrical constraints, step B"):

- **Amplitude.** KiCad writes `max_amplitude: 1.0` into every project, so the project value is the *starting*
  amplitude: when the plan names no `groups[].amp` and a member stays short, the net is laid again at 2 × and
  4 × (never above 4 mm) and the attempt that ends longest is kept. A plan `amp` is a hard limit.
- **Ladder and survey.** Amplitudes `a_max, ¾, ½, ⅜, ¼, … a_min`; the survey slides along the run in 0.1 mm
  steps (first fit) instead of fixed slots every `F + S`; ties between the two sides keep the previous bump's
  side.
- **Which run.** The run that covers the whole need with the fewest bumps (ties: the longest); when none can, the
  one with the largest capacity first. Allocation takes the tallest slots first, then lowers amplitudes from the
  last bump backwards; a remainder below one U bump goes to one or two shallow bumps.
- **Own-net rule.** Feet are checked against the net's own pads and vias, legs and tops against all own copper
  (`S − w`, edge to edge); a run with other copper of its net attached to its interior is skipped.
- **Group order (§14.6 item 1).** Priority 70 alone made `cons_bus_match` worse (2 vias, target 36.7 mm). As
  built the members of a tuned group form one block, **longest estimate first**, at priority `max(own, 70)`
  (`classify.GROUP_BIAS`): target 29.7 mm, 0 vias and 16 mm of meander, against 2 vias and 33 mm.
- **`tune_pass`.** A net KiCad rejects in each of the three attempts is given up alone (it stays as routed and
  says so); the other nets keep their meanders. At most five KiCad round trips, one in the normal case.

*[as built, fix round 1]* **A member with no room beside it changes layer.** The shape above can only add
length beside an existing run on the run's own layer; a track with a neighbour at the clearance on both sides
(`cons_mixed` `/Q_CLK`: `capacity 0.0`) stayed short on a board whose other layers were empty. Now, when every
amplitude on the net's own layers leaves a member short, a stretch of ONE straight run moves to another layer
the net may use, through a via at each end ("hop", `tune._hop`): either the run itself with its bumps
(`_hop_line`), or — on a short run — two vias 1 mm apart with one loop at right angles between them
(`_hop_loop`, gain `2A - 2c(2 - sqrt 2)`, legs straight out of the via centres so that KiCad's in-via shortcut
takes nothing off). The vias' stack height is length (§14.5) and is part of the allocation; a hop whose vias
alone would overshoot is not laid. Vias keep the clearance from the net's own pads, stay out of forbidden areas
with their whole pad, respect `nets[..].max_vias` / KiCad `via_count` / `regions[].no_vias`; at most two hops
per net; the result says so (`groups[].hops`, a note naming the nets and layers). `tune_pass` takes hops out
first when KiCad reports something nobody can be blamed for. Skew bumps: the aim never lies inside the tuner's
guard below the longer net (it did for limits under 0.008 mm, and every bump was undone), lower bumps are tried
where a high one does not fit. The tuner's copper stays out of the plan's forbidden areas with its whole width.

Not in this step: meanders on arcs or on existing copper; reserving room for meanders while other nets are still
being routed (optional step B5: a soft `"reserve"` rectangle beside the longest run of a member whose estimate is
short, priced by `window.SOFT_PRICE`, removed before the pass); serpentines of a *coupled* pair (both lanes
together) — a pair that is a group member and too short is reported with its shortfall.

### 14.8 Coupled pair routing (`v2/pairs.py`) — engineer C

Decision: **centre line first** ("one object"), not route-then-couple. Route-then-couple (today's N-after-P, then
pull N towards P) cannot guarantee the gap through bends and gives up wherever a third net already sits between
the two. The centre line needs no new search: the whole ladder of `detail.route_conn` (pattern → corridor windows
→ wider → bounded wide search) runs on a virtual connection.

```python
def jobs(ctx) -> list[PairJob]                       # coupled pairs whose P and N connections match 1:1
def gather(ctx, pa: Terminal, na: Terminal, toward) -> Gather | None
def route_pair(ctx, job, budget) -> dict             # {ok, coupled, pieces, why}
def offset(points, d) -> list                        # octilinear polyline shifted by d (mitred corners)
def coupled_stats(ctx, pair) -> dict                 # {coupled_mm, uncoupled_mm: [p, n], gap_violations}
def partner(ctx, conn_idx) -> int | None
```

1. **Job**: both nets have the same number of open connections and they pair up end for end (P.a / N.a on one
   footprint, P.b / N.b on another). Anything else (tees, series parts splitting only one net) → sequential.
2. **Gather point** at each end: mid-point `M` of the two terminals, lane spacing `s = w + gap`, exit direction
   towards the other end. The fan-in is the shape KiCad accepts as coupled (§14.9 "fanin"): each track leaves its
   pad centre on a **diagonal straight to its lane** (no parallel straight exit: that is a max-gap error when a
   rule has a `max`), and each lane is **broken into two segments at the first point where the rung between the
   lanes is clear of every other copper, the pair's own pads included** (KiCad discards a whole lane segment as
   uncoupled when the rung at either end touches a pad). The gather point `G` is that point; candidates
   `d = d0 + k × 0.1 mm` along the exit direction, first one whose fan-in pieces pass the oracle for both nets.
   Pads on opposite sides of the travel direction at the two ends (a twisted pair) or no legal fan-in → sequential.
3. **Centre line**: `vc = dataclasses.replace(conn_p, a=Terminal(Ga), b=Terminal(Gb), width=2w + gap + 0.05,
   via=(2 × via + via_gap, drill), tree_target=False)` goes through `detail.route_conn(ctx, vc, budget,
   accept=…)`. New keyword `accept` (also on `_route_window`, and after `pattern()`): instead of
   `state.add(**spec)` the specs go to `pairs._accept(ctx, job, specs)`, which returns the added pieces or a
   `Blocker` (then `_block_reject` closes those cells and the search runs again, at most `RESEARCH` times).
4. **`_accept`**: split the centre specs into per-layer polylines; `offset(±s/2)`; at a centre via with travel
   direction `u`: two vias at `±pv/2` across `u`, `pv = max(via + via_gap, drill + hole_to_hole, s)`, joined to
   the lanes by diverging 45° pieces (no parallel run at via pitch: measured as a max-gap error and as uncoupled
   length), and the centre line must run straight through the via (a turn at the via is rejected back to the
   search); break lanes where a rung would touch foreign or own copper; prepend / append the fan-ins. **Every**
   resulting segment and via is checked by `oracle.seg_ok / via_ok` as its own net — the fat search width is only
   an accelerator (a mitred outer corner leaves its envelope by 0.04 × s). Then one transaction: P pieces with
   `conn = P.idx`, N pieces with `conn = N.idx`, `stage = "detail"`; commit only when `state.connected` holds for
   both. `ctx.history["pairs"][p] = {coupled, lanes, uncoupled, why}`.
5. **Fallback**: on any refusal the two connections are routed by `route_conn` one after the other exactly as
   today; the report says `coupled: false` and why (`pads swapped at J1`, `no legal fan-in at U3`, `blocked at
   (x, y)`). A pair therefore always routes when its nets would have routed before.
6. **Afterwards**: the two nets are treated as plan-protected (`repair._net_protected`: repair never rips them
   for another net; veto `nets[..].protect: false`); `engine.reroute(ids)` (the judge's re-route) adds
   `partner()` and calls `route_pair` first; polish keeps skipping role `pair`. Intra-pair skew (each 45° bend
   costs the inner lane `2 s tan 22.5° = 0.83 s`) is tuned by `tune.run(mode="skew")`: shallow bumps
   (`h ≤ min(3 × gap, 0.3)`) on the outer side of the shorter net, nearest its pads first, inside
   `judge.tune_pass`.
7. **Pairs KiCad does not know** (`Pair.kicad == False`, e.g. `/USB_DP` + `/USB_DM`): the oracle applies the
   ordinary clearance between them, so the lanes sit at `max(gap, clearance)` and the report says so with the
   fix (rename to `…_P/_N` or `…+/-`).

**Width classes** (C): `classify.widths()` asks `cons.widths` first; `ConnV2.width = max(by_layer, width)` (windows
are built for the widest), `neck = width`; `detail.emit()` writes `w = cons.width_on(net, layer)` and
`target_w = w` (polish `widen` then leaves it); the oracle check in `emit` already uses the piece's own width.
`constraints.z_estimate(stack, layer, w, gap)` (closed-form microstrip / stripline, only when the stackup gives
εr and a neighbouring plane layer exists) feeds the report; a deviation above 15 % from the label is an
`IMPEDANCE_ESTIMATE` info finding. **Keep-short** (C): order as today; `max_length` becomes hard again (v1's
contract in the field text) through `constraints.enforce_max_length`; a soft keep-short net longer than
`1.3 × est + 1 mm` is a `critique` row `keep_short_detour`.

*[as built, step C]* Built as above with these differences (details and measurements:
`docs/internal/v2-interface-notes.md`, "Electrical constraints, step C"):

- **The centre-line search stands for both nets.** While it runs, the oracle treats the N net's copper as the P
  net's own (`Oracle.set_ally`); with the N pads as foreign copper no gather point existed at a fine-pitch part.
  `_accept` switches that off and checks every lane piece as its own net.
- **Fan-in.** A straight lead out of the pad comes before the diagonal when the diagonal would lay a lane exactly
  on a pad edge (KiCad's length figure is then a toss-up per net: §14.9). Pads with a fan-out escape are tried
  from their centres first.
- **Vias.** The two vias sit across the bisector of the incoming and outgoing direction, so a turn of up to 90
  degrees at the via is laid; sharper turns go back to the search.
- **Corners.** A centre piece too short for its two mitres is dropped from the inner lane instead of being
  rejected (the lanes are then further apart than the gap for that corner, never nearer).
- **Skew at once.** The bumps are laid right after the pair, while the room beside it is free, not only in the
  judged pass at the end (which then adds what is still missing, and undoes bumps that cost the pair a KiCad
  pair rule). On `cons_usb_pair` the judged pass alone could add nothing.
- **Completion first.** A board that does not end complete and clean, with pairs that were only detected (names
  / classes) laid as lanes, is routed once more with those pairs as two nets; the better result by KiCad's
  verdict is kept and says so (`engine._pairs_opinion`). Measured on `cm5_minima`: 198 / 208 coupled, 199 / 208
  as two nets. Plan- and rule-backed pairs are never un-coupled.
- **`max_length`.** Only the plan's is enforced by failing a connection; a KiCad `length (max ..)` rule is
  reported with its excess.
- Not built: `z_estimate` / `IMPEDANCE_ESTIMATE`, the `critique` kinds, the retirement of `out["diff_pairs"]`,
  per-layer widths for the nets of a pair.

### 14.9 Measured KiCad behaviour (KiCad 10.0, kicad-cli DRC, `bench/route/cons_probe.py`)

Rerun: `"C:/Program Files/KiCad/10.0/bin/python.exe" bench/route/cons_probe.py` (nine cases — `midpad` and `viastub`
were added in step A — seconds; writes only
under `bench/route/.work/cons_probe/`; each case prints KiCad's errors and the expected figures). Everything below
was observed on 2026-11-11 on this machine.

| Fact | Evidence |
|---|---|
| kicad-cli reads `<board>.kicad_dru` and `.kicad_pro` next to the board; `A.NetName == '/SD_*'` matches by glob | every case |
| Message formats | `Track length out of range (rule 'R' min length 35.0000 mm; actual 30.0000 mm)` · `Skew between tracks out of range (rule 'R' max skew 0.1000 mm; actual -0.4971 mm; target net length 30.4971 mm (from /D_N); actual 30.0000 mm)` · `Differential pair gap out of range (R maximum gap 0.2500 mm; actual 0.8000 mm)` · `Differential uncoupled length too long (R maximum uncoupled length 3.0000 mm; actual 5.0556 mm)` · `Too many vias on a connection (rule 'R' max count 1; actual 2)`; items read `Track [/NET] on F.Cu, length …` / `Pad 1 [/NET] of J1 on F.Cu` |
| Skew is each net against the **longest** net the rule matches (group and pair alike) | `pair`: `/SD_CLK` −3.0627 against `/SD_D0` |
| A net's length is the sum of all its tracks (tee stub included) | `hops` `/T`: 30 + 2 = 32.0000 |
| Via height counts between the layers used, 4-layer default stack (1.6 mm): F–In1 0.5325, F–In2 1.0475, F–B 1.58; 2-layer default: 1.58 | `hops` `/A /B /C`; a via in an SMD pad counts too (`via`: 30.4556 + 2 × 1.58) |
| Pad-to-die is added | `hops` `/E`: 30 + 2.5 |
| In-pad shortcut only for a chain ending on the pad's position | `padentry`: `/c` 30.1657 → 30.0649, `/i` 30.2071 → 30.1000; `/b /d /e /h` counted in full |
| A lane exactly on a pad edge is counted either way | `padentry` `/f` 30.1617, mirror image `/g` 30.2485 |
| U bump adds `2a − 4c(2 − √2)` | `pair` `/SD_D0`: 33.0627 expected and reported |
| An unrouted net: skew rules silent, `length (min)` fails with actual 0, `length (max)` silent | `unrouted`, `hops` `/U` |
| A lane segment is coupled only if the rungs at both its ends are clear of other copper, own pads included | `fanin` c: whole 30.2 mm uncoupled; e: 0 errors |
| A straight parallel exit at pad pitch, or a parallel jog at via pitch, is a max-gap error (and uncoupled) | `fanin` d, `via` |
| Uncoupled length is per net: the sum of its segments that are not coupled within the gap window | `via`: 5.0556 = the fan-ins and jogs of one net |
| *[as built, step A]* Two chains ending on the same pad centre on one layer are one line through the pad: no in-pad shortcut (it applies where exactly one segment end is on the pad position) | mid-chain pad with a dogleg on both sides: 30.3234 = the plain sum |
| *[as built, step A]* A via gets the same shortcut as a pad: a chain ending on the via position is counted as the straight line from where it enters the via's copper circle (radius r) to the centre; a turn `s` before the via costs `s + sqrt(r² − s²) − r` | routed `cons_mixed` `/Q_IO1`, stub s = 0.01 / 0.035 / 0.05 / 0.1 / 0.2: 21.0585 / 21.0426 / 21.0342 / 21.0126 / 21.0055; s = 0.3 = r and 0.5: in full |
| *[as built, step A]* A via in the middle of a straight track with a branch on another layer counts both layers; In1–In2 on the default 4-layer stack is 0.515 | 33.0 + 1.58 = 34.58; 30 + 0.5325 + 0.515 + 1.0475 = 32.095 |
| *[as built, step A]* `pcbnew.SaveBoard` overwrites a `.kicad_pro` next to the board: net classes must be merged into the file it writes | first build of the constraint boards: every net in class Default |

Not measured: `(constraint skew … (within_diff_pairs))`, inner-to-inner via hops, explicit stackups with
prepreg / core mixes, blind / buried vias, arcs, KiCad 9, a non-English KiCad (message texts).

### 14.10 Measurement and reporting

`out["constraints"]` (built by `constraints.measure`, attached in `engine._Run.result()`, passed through
`session._route_v2`; replaces `out["diff_pairs"]` for engine v2):

```
{"mode": "auto", "ok": 3, "missed": 1,
 "groups": [{"name": "sd", "n": 5, "target": 41.20, "min": 40.98, "max": 41.20, "worst": "/SD_D2", "dev": -0.22,
             "tol": 0.5, "ok": true, "src": "dru", "tuned": 4, "added": 6.4}],
 "pairs":  [{"p": "/USB_D_P", "n": "/USB_D_N", "len": [30.12, 30.10], "skew": 0.02, "max_skew": 0.15,
             "gap": 0.2, "coupled": 96, "uncoupled": 1.3, "ok": true, "src": "name"}],
 "short":  [{"net": "/XI", "len": 4.1, "max": 8.0, "ok": true}],
 "widths": {"n": 6, "off": [["/CLK", "In2.Cu", 0.15, 0.11, 3.2]]},
 "kicad":  {"skew_out_of_range": 1}}
```

- A missed target always carries the amount: `dev` (mm, signed: negative = short of the window), and for a tuned
  group `short_by` / `long_by` with `why` (`"no free run: capacity 0.6 mm of 2.4 needed"`, `"meander rejected by
  KiCad at (x, y): clearance"`, `"open connection"`, `"tolerance below the tuner's 0.07 mm resolution"`).
  `ok` is KiCad's verdict when it gave one for those nets, else the router's figure against the window.
  A group in `report` mode has `ok: null` and `src: "name"`: it is information, not a pass.
- Compact form (`route_tools._compact_v2`, the 6 000 B cap of `tests/test_tokens.py`): misses first, at most
  `CONS_MAX = 6` rows per kind, passed rows beyond that collapse into the counts, `more` names
  `constraints (+n rows)`; lengths rounded to 0.01 mm; per-net figures (`nets: {net: [total, track, via, die,
  vias]}`) only with `detail=true`. A board without constraints has no `constraints` key at all.
- `drc.new_errors` stays KiCad's truth (copper + constraint errors); `drc.constraint_errors` says how many of them
  are of `KICAD_TYPES`, so `DRC_ERRORS_NEW` ("could not repair or attribute") is raised only for the rest.
- Findings (`route_tools._cons_findings(out)`, each with a fix and, where one exists, a patch in `suggest[]`):

| Code | Severity | Fix hint |
|---|---|---|
| `LENGTH_MISMATCH` | warning (`src` plan / dru), info otherwise | "group sd: /SD_D2 is 1.8 mm short of 41.2 (tol 0.5); free runs can add 0.6 mm" → move parts apart or `regions` cost round its runs, raise `groups[].amp`, relax `tol`, or set `len` to the reachable value (given) |
| `PAIR_SKEW` | warning when `max_skew` is exceeded | by how much; which bend caused it; `skew` to relax |
| `PAIR_UNCOUPLED` | warning | coupled share, uncoupled mm against the limit, why (swapped pads at J1 / no fan-in at U3 / blocked at (x, y)); swap P and N at the connector, move the part, `coupled: false` to accept |
| `PAIR_NOT_KICAD` | info | the names are no pair for KiCad: rename to `_P/_N` or `+/-` to get its pair rules |
| `KEEP_SHORT_LONG` | warning | routed length against `max` / the estimate; `order`, placement |
| `WIDTH_NOT_HELD` | warning | net, layer, width laid against the rule, mm affected |
| `CONSTRAINT_GUESSED` | info | the patch that turns a name-detected group into a tuned one |
| `RULE_NOT_READ` | info | a KiCad rule the router could not read; KiCad still checks it |
| `KICAD_CONSTRAINT_ERRORS` | error | KiCad reports constraint errors the router's own figures did not predict (types and nets) |

`diag.critique` gains the kinds `length_spread`, `pair_skew`, `pair_uncoupled`, `keep_short_detour`
(`route_tools._QUALITY` gets their one-line texts); `planx.auto_preview` and `pcb_route_plan`'s `auto` gain
`constraints` (what detection found: groups / pairs / short with `src`, `conf`, `why`), omitted when empty.

### 14.11 Benchmark

Four synthetic boards, generated by `bench/route/prepare.py` next to `synth_bus_cross` (same `op_synth`; the spec
gains `"pro": {...}` and `"dru": "<text>"`, written as `<name>.kicad_pro` / `.kicad_dru`; `meta.json` gains
`"suite": "cons"` and `"constraints": {groups, pairs, short}` = the expected set). `run.py` gets
`--suite route|cons|all` (default `route`: the 14-board table and its totals stay comparable with
`v2_stage7b`). The skew / pair / max rules are chosen so the **unrouted** board has no constraint error (§14.9).

| Board | Layers | Content | KiCad rules in the board's `.kicad_dru` |
|---|---|---|---|
| `cons_usb_pair` | 2 | connector J1 (6 pads, pitch 1.0) at the west edge → MCU row U1 (8 pads, pitch 0.65) rotated 90° to it, `/USB_D_P`, `/USB_D_N` on neighbouring pads, one 90° turn on the way; 4 ordinary nets (one of them 0.5 mm wide) | class `USB` (`/USB_D_*`): width 0.2, pair gap 0.15 (below the 0.2 clearance: KiCad's implicit pair rule); `inDiffPair('/USB_D_')`: `skew (max 0.15mm)`, `diff_pair_gap (min 0.15mm) (max 0.9mm)`, `diff_pair_uncoupled (max 6mm)` |
| `cons_bus_match` | 2 | J1 (column, pitch 1.27) → U1 (row, pitch 0.8), `/SD_D0…3` + `/SD_CLK` + one unconstrained net; pad order chosen so the natural lengths differ by ≥ 6 mm (asserted from `meta.facts.mst_mm` when the board is generated); open area beside the bus | `A.NetName == '/SD_D?' \|\| A.NetName == '/SD_CLK'`: `skew (max 0.5mm)` |
| `cons_pairs_cross` | 2 | pair A west → east, pair B north → south, SMD pads: the two must cross, so one changes layer through a via pair twice | per pair: `skew (max 0.2mm)`, `diff_pair_gap (min 0.15mm) (max 1.2mm)`, `diff_pair_uncoupled (max 8mm)` |
| `cons_mixed` | 4 (In1 GND pour) | QFP-32 MCU; QSPI group `/Q_IO0…3` + `/Q_CLK` to an 8-pad memory; a USB pair to a connector; crystal `/XI`, `/XO`; 12 ordinary nets to a reversed 2.54 mm header; 3V3 | QSPI: `skew (max 0.3mm)`; USB pair as above; `/XI`, `/XO`: `length (max 8mm)` |

Harness metrics (new `bench/route/cons.py`, independent of the router's report like everything else there):

- `kicad_len[net]`: KiCad's own length of every constrained net, from a throw-away copy of the routed board with
  an always-failing rule `(constraint length (min 2000mm))` per net and the `actual` of the resulting markers
  (`common.kicad_lengths(pcb, nets)`; the mechanism of `cons_probe.py`).
- per group: `spread = max − min` of `kicad_len`, `ok = spread ≤ tol`; per pair: `skew`, via counts of P and N,
  `coupled_pct` = share of the longer net's track length that has a parallel partner segment on the same layer at
  the target gap ± 0.02 mm (geometry of the measured segments); per keep-short net: length against `max`.
- `cons_drc`: the five constraint types in the board's own DRC, counted separately and inside `drc_new_errors`.
- `len_model_err = max |router figure − kicad_len|`; `detect`: the router's `auto.constraints` with **no plan**
  against `meta.constraints` (missing / extra); `meander_mm` and wall time against a run with
  `--opt plan='{"global":{"constraints":"off"}}'`.

Pass thresholds (`run.py --router v2 --suite cons --runs 2`; a board passes only if all hold):

| Metric | All four | Board-specific |
|---|---|---|
| done % | 100; `cons_mixed`: ≥ its `constraints: "off"` run | |
| new DRC errors, constraint types included | 0 | |
| same twice | yes | |
| detection with no plan | nothing missing, nothing extra | |
| `len_model_err` | ≤ 0.005 mm | |
| group spread (KiCad lengths) | ≤ tol | `cons_bus_match` 0.5, `cons_mixed` 0.3; no member more than 2 µm above the longest untuned one |
| pair skew | ≤ rule | 0.15 / 0.2 |
| `coupled_pct` | | `cons_usb_pair` ≥ 70, `cons_pairs_cross` ≥ 60 per pair, `cons_mixed` ≥ 50 |
| vias P = vias N | yes | |
| wall time | ≤ 1.5 × the `off` run + 10 s | |

A missed threshold is a result to report (per board, per metric, with the mm), not a reason to loosen the rule file.

### 14.12 Build steps — three engineers, one after another on the same files

Rules for all three: KiCad's Python, `PYTHONPATH=src`, fresh short `COPPER_HOME` / `KICAD_CONFIG_HOME` under
`bench/route/.work/`; no vendor names in `src/` (`tests/test_neutral.py`); no longer tool description or schema;
never weaken an existing safety assertion; new behaviour gets new tests. **Gate after each step**: every
`tests/test_v2_*.py`, `tests/test_guided_route.py`, `tests/test_tokens.py`, `tests/test_protocol.py`,
`tests/test_neutral.py` pass (one file at a time), and the no-constraint boards hold as stated per step.
Reference signatures in `results/v2_stage7b/*.json` (`runs[0].signature`): `synth_bus_cross` `df305b8fa9397c38`
(1132.88 mm, 40 vias), `phone_breathalyzer` `bf7124da602b3acd` (314.5 mm, 19 vias) — the tree has moved since
that run, so step A0 re-measures them first.

**Step A — model, detection, measurement, reporting, benchmark boards, tests** (no copper changes)

| # | Work | Files | Acceptance |
|---|---|---|---|
| A0 | Record the "before": `run.py --router v2 --name cons_before --board synth_bus_cross --board phone_breathalyzer --runs 2` | `bench/route/results/cons_before*` | two signatures, both deterministic, written into the step report |
| A1 | Plan syntax: `GROUP_FIELDS`, pair extras, `widths`, `z`, `global.constraints`; `V2Cfg.cons_plan / cons`; `SCHEMA_V2` line; `merge_patch` | `v2/planx.py`, `tests/test_v2_planx.py` | every field accepted and resolved; each bad value / unknown key / net in two groups → `BAD_PLAN` with the fix; guided engine flags them; `test_defaults_are_identical_to_no_plan` still green |
| A2 | Model + project reader + detection (`read_project`, `parse_dru`, `resolve`, `preview`) | new `v2/constraints.py`, new `tests/test_v2_constraints.py` | DRU subset parsed on the four bench rule files and on `phone_breathalyzer.kicad_dru` (no constraint found there, nothing in `unread` but true unknowns); precedence plan > dru > class > name; `synth_bus_cross`: **no** group, no pair; `phone_breathalyzer`: one pair `kicad: false`; `cm5_minima`: its `_P/_N` pairs with class dimensions; a name group is `mode: "report"` |
| A3 | Measurement (`Stackup`, `die_lengths`, `net_length`, `measure`, `kicad_markers`) | `v2/constraints.py` | unit tests reproduce §14.9: via hops 0.5325 / 1.0475 / 1.58, die +2.5, tee 32.0, pad entry `/c` 30.0649 and `/i` 30.1000 on model boards built from the same geometry; the five message formats parse; an unparsable message keeps type + nets |
| A4 | Engine / judge wiring: resolve in `_Run.setup`, group priority in `classify._connections`, `judge._check` split (`chk["cons"]`), `out["constraints"]`, `auto.constraints` | `v2/engine.py`, `v2/classify.py`, `v2/judge.py`, `guided/session.py`, `tests/test_v2_judge.py`, `tests/test_v2_engine.py` | scripted judge test: a `length_out_of_range` / `skew_out_of_range` marker removes **no** connection and appears in `constraints.kicad`; existing judge tests unchanged |
| A5 | Tool layer: `_cons_findings`, compaction, `_auto` preview, `add_help` text | `tools/route_tools.py`, `tests/test_tokens.py` (new cases only) | result with 12 groups + 12 pairs stays ≤ 6 000 B compact; catalogue size unchanged to the byte |
| A6 | Benchmark: four boards + rule files, `--suite`, `cons.py` metrics, `common.kicad_lengths` | `bench/route/{prepare,run,common,cons}.py`, `bench/route/corpus/cons_*`, `README.md` | unrouted boards: 0 constraint errors; detection metric passes on all four with no plan; `--suite cons` table written (`results/cons_A.md`): the misses are listed with mm — tuning and coupling do not exist yet, so spread / coupling thresholds are expected to fail and must be **reported as failing** |
| Gate A | | | `--board synth_bus_cross --board phone_breathalyzer --runs 2`: signatures **identical** to A0, 0 new DRC |

**Step B — length tuning** (built 2026-11-11: B1 – B4; B5 not built; results `bench/route/results/constraints_tune.md`)

| # | Work | Files | Acceptance |
|---|---|---|---|
| B1 | `tune.py`: geometry (`gain`, bump points on axial and diagonal runs), survey, allocation, placement, `untune`; `types.STAGES_OF_PIECE += ("tune",)` | new `v2/tune.py`, `v2/types.py`, new `tests/test_v2_tune.py` | unit: gain formula to 1 nm; a 6 mm deficit on a free 30 mm run lands inside a 0.1 mm window; every added segment `oracle.seg_legal`; bumps keep `S − w` from own copper; nothing enters a `forbid` region or an `avoid` rectangle (new test beside the existing forbid tests); a run teed onto is not touched; `untune` restores piece-for-piece; same twice |
| B2 | `judge.tune_pass` + engine call + pass record + budget share; polish / revia skip tuned connections | `v2/judge.py`, `v2/engine.py`, `v2/polish.py`, `tests/test_v2_judge.py` | scripted KiCad: a marker on a tune piece untunes only that net and the next round tunes it elsewhere; a marker nobody can attribute restores the accepted board; opens never rise; the calibration round moves a net by KiCad's reported difference |
| B3 | Reporting of tuning (`tuned`, `added`, `short_by`, `why`), `LENGTH_MISMATCH` fix text and patch, frugal / alt keys | `v2/constraints.py`, `v2/engine.py`, `tools/route_tools.py` | a group that cannot be met (run fenced in by a `forbid` region in the test) reports the shortfall in mm and the capacity; never `ok: true` |
| B4 | Bench | `results/cons_B.md` | `cons_bus_match` passes every threshold; `cons_mixed` group passes or is reported with mm; `len_model_err` ≤ 0.005 on all four |
| B5 | (optional, only if B4 shows capacity misses) soft reserve beside short members during routing | `v2/tune.py`, `v2/detail.py` | no board of the `route` suite changes signature |
| Gate B | | | as Gate A: both signatures identical to A0 (no tuned group on either board) |

**Step C — differential pairs, width classes, keep-short** (built 2026-11-11: C1 – C3, C5, the findings and
help text of C4; results `bench/route/results/constraints_final.md`)

| # | Work | Files | Acceptance |
|---|---|---|---|
| C1 | `pairs.py`: `jobs`, `gather` (diagonal fan-in, lane break at the clear rung), `offset`, `_accept`, `coupled_stats`; `detail.route_conn / _route_window(accept=)`; dispatch in `detail.run` | new `v2/pairs.py`, `v2/detail.py`, new `tests/test_v2_pairs.py` | unit: offset of L / Z / 45° polylines keeps the gap to 1 nm; straight, one-bend and one-via pair on synthetic boards: every piece oracle-legal, both nets connected, one transaction (rollback leaves nothing); swapped pads and a fenced-in fan-in fall back to sequential and both nets still route; `route_conn` without `accept` byte-identical (existing detail tests) |
| C2 | Protection, judge re-route with partner, `tune.run(mode="skew")` | `v2/engine.py`, `v2/repair.py` (read only: uses `x.net(n).protect`), `v2/tune.py` | a judge marker on one lane re-routes both; skew bumps only on the outer side, uncoupled length reported after them |
| C3 | Width rules per layer, no necking; `enforce_max_length`; keep-short detection additions; critique kinds | `v2/classify.py`, `v2/detail.py` (`emit`), `v2/constraints.py`, `v2/diag.py` | a net with `widths` lays each layer's width, polish does not re-widen it; a net over `max_length` is failed with the excess in mm and never written longer |
| C4 | Tool text: the `diff_pairs` line of the full description reworded to the same or a shorter length; findings `PAIR_*`, `WIDTH_NOT_HELD`, `KEEP_SHORT_LONG`; `out["diff_pairs"]` retired for v2 | `tools/route_tools.py`, `guided/session.py` | catalogue ≤ 58 000 B; `PAIR_SKEW` with a limit says by how much |
| C5 | Bench | `results/cons_C.md`, full `route` suite as `results/v2_cons.md` | `cons_usb_pair` and `cons_pairs_cross` pass or are reported per metric; full corpus: no board below `v2_stage7b` done %, 0 new DRC, 14 / 14 same twice (`cm5_minima`, `stickhub`, `one_air_max`, `tinytapeout`, `phone_breathalyzer` carry name pairs and will change copper) |
| Gate C | | | `synth_bus_cross`: signature identical to A0. `phone_breathalyzer` (one name pair, now coupled): 44 / 44, 0 new DRC, same twice; per-net length diff against A0 listed in the step report |

### 14.13 Risks, and what this design does not do

| Risk / gap | Consequence | Mitigation in the design |
|---|---|---|
| The router's length figure disagrees with KiCad's (pad-edge toss-up, kept arcs as chords, explicit stackups, inner-to-inner hops not measured) | a tuned net lands just outside a tight window | `tune_pass` closes the loop on KiCad's reported `actual` (`cal`); bench metric `len_model_err`; lanes and bumps keep off pad edges |
| KiCad message texts are parsed (English, KiCad 10.0) | another language or version: no numbers from KiCad | types and nets still counted; the router's figures are used; stated in the report (`kicad: "unparsed"`) |
| No free straight run where length is needed (dense boards; nets routed after the group packed in beside it) | target missed | reported per net with capacity; optional reserve (B5); `groups[].amp`; the finding names the reachable `len`. *[fix round 1]* a stretch on another layer through two vias (§14.7) when the net's own layers have no room; still missed when no layer has room, when the two vias alone would overshoot, or when `max_vias` forbids them — reported |
| A hop lands on a layer with a pour and cuts part of it off | a pad of the poured net loses its pour | KiCad judges the tuned board after refilling; hops are taken out first when it reports an open connection nobody can be blamed for (`tune_pass`); the layer is NOT chosen by what is poured there (not built) |
| Coupled pair search at `2w + gap` fails more often than two single searches (fine-pitch parts, fan-out stubs, congested corridors) | fewer coupled pairs than a human gets; on `cm5_minima` possibly fewer connections because protected pairs block others | per-pair fallback to today's routing; protection is vetoable; C5 gate is "no board below `v2_stage7b`" — if it fails, coupling stays opt-in (`coupled: true` in the plan) and that is reported |
| Via transitions of a pair (two vias, straight-through rule, 45° divergence) are the least proven part | `cons_pairs_cross` may fail its coupling threshold | each via pair is checked piece by piece; fallback; the board exists to measure exactly this |
| KiCad's coupling test works per segment with clear end rungs (measured on three fan-ins, not on vias or bends) | a lane we count as coupled is uncoupled for KiCad | lane breaks at every rung that touches copper; `coupled_pct` (geometry) and KiCad's `diff_pair_uncoupled` are both reported; KiCad's is the verdict |
| A `diff_pair_gap (max)` tighter than the pad pitch cannot be met at the pads by any router | constraint errors at the footprint | predicted by `coupled_stats` and reported as `PAIR_UNCOUPLED` "at pads of U1", not hidden |
| `judge._check` change touches every board with KiCad length / pair rules | a net that KiCad flags as too short is no longer ripped | intended; unit-tested; the corpus has no such rules (`phone_breathalyzer.kicad_dru` has none) |
| Auto pair detection changes copper on boards with name pairs (step C) | not byte-identical to `v2_stage7b` there | gate is connections / DRC / determinism; `global.constraints: "report"` reproduces the old copper |
| Extra KiCad round trips | + 1 … 3 per run with tuned groups, also inside each via-frugal rung | only when `cons.tuned()`; wall-time threshold in §14.11 |

Not designed here, and to be said plainly in every report that touches them: per-branch (pin-pair) matching of
multi-load nets (KiCad's rule cannot express it either); serpentines of a coupled pair as a pair; delay- instead of
length-matching and KiCad 10 tuning profiles; return-path continuity (routing a constrained net across a split in
its reference pour) and reference-plane layer choice; arcs; blind / buried vias; impedance *synthesis* (ohms →
width); meanders on copper that was on the board before the run.

## Appendix A — measured during stage 0 (W0)

Everything in A1 and A2 was measured with KiCad 10.0 on this machine; how to rerun is given with each part. Nothing
here is a projection.

### A1. Root cause of the `_verify` failure at (126.51, 81.42) on `one_air_max`

**Method.** v1's `guided.route` was run unchanged on a scratch copy of the corpus board with the suggested plan
(as the benchmark does), with `Router._verify` wrapped to record, for each connection it is about to drop, the
failing segment, whether its ends are grid points, and every shape it violates (static or routed) with distances.
The run reproduced the baseline exactly: 273/316 routed, 75 connections reported as "internal clearance self-check
failed", GND × 37 at (126.51, 81.42) and +3V3 × 38 at (121.41, 79.62). The same trace on `coldfire` (default
budget) reproduced its 3 self-check failures.

**What fails.** In each affected pass exactly **one** connection fails the exact check, on exactly one segment:

| Board, pass | Connection | Failing segment (B.Cu / F.Cu), width | Offender | Centre distance vs `hw + clearance` | Verdict by the rule |
|---|---|---|---|---|---|
| one_air_max, pours (twice) | GND `U3.2→pour` | (126.21, 81.12)→(126.81, 81.72), 0.25 | corner of pad `U3.3` (other net, static) | 0.22274 vs 0.225 | **2.26 µm short — a real violation** |
| one_air_max, pours (twice) | +3V3 `U12.8→pour` | (121.11, 79.92)→(121.71, 79.32), 0.25 | corner of pad `R6.2` (other net, static) | 0.22627 vs 0.225 | **legal, 1.27 µm to spare**; fails only v1's extra `+0.002` |
| coldfire, signals | `/DSI` `U102.79→R109.2` | (143.92, 106.08)→(144.12, 106.28), 0.2 | rounded corner of pad `U102.78` | 0.24744 vs 0.25 | 2.56 µm short — real |
| coldfire, signals | `Net-(COM_SEL201-P3)` | (108.12, 77.68)→(107.72, 77.28), 0.2 | corner of pad `COM_SEL201.1` | 0.24961 vs 0.25 | 0.39 µm short — inside KiCad's 0.5 µm epsilon |

The reported coordinate (126.51, 81.42) is simply the midpoint of the first segment.

**Root cause.** All four segments are **on-grid 45° runs: both ends are cell centres**. No snap segment, no escape
stub, no via and no routed copper is involved (every offender is a static pad of another net) — the suspicion in
§4.1 was wrong. `grid.py` blocks a cell when its *centre* is closer than `half_width + clearance + EPS`
(`EPS = 0.012`) to foreign copper and assumes a straight step between two free neighbours "can dip into a halo by
only a few micrometres". That holds only for fine grids: past a convex corner the chord between two free diagonal
neighbours comes closer by up to `sqrt(reach² + p²/2) − reach` (`reach = half_width + clearance`, `p` = grid pitch):

| Board | p | reach | worst dip of a diagonal step | EPS |
|---|---|---|---|---|
| one_air_max | 0.15 | 0.225 | 23.7 µm | 12 µm |
| coldfire | 0.20 | 0.25 | 37.2 µm | 12 µm |
| typical small board | 0.10 | 0.30 | 8.2 µm | 12 µm (enough) |

So on boards whose auto grid is coarse relative to track + clearance (the big, dense ones), A* legitimately finds
paths that are up to ~12–25 µm inside the rule. The exact check exists in v1 but runs only once, at the very end of
a pass.

**Why 75 connections were lost for two segments.** `_verify` calls `rip_net(c.net)`: the whole net, not the
connection. On a pour net every pad-to-pour stub is its own connection, so one bad stub of GND drops 37 routed
connections and one stub of +3V3 drops 38 — and the +3V3 stub was not even a violation (it fails only the 2 µm
safety margin `seg_clear` adds on top of the rule). The run is deterministic, so the repeat pours pass after the
DRC repair loop loses them again. The `coldfire` 600 s case (+3.3V × 87 at (144.02, 105.18)) was not re-traced; it
sits next to the same `U102` pad row as the traced `/DSI` failure.

**Consequences for v2** (and what was done about them):

1. `legacy.py` (W1) must rip the failing **connection** only, and should judge with the oracle (rule − `TOL_MM`)
   rather than `seg_clear` (rule + 2 µm): of the four traced failures one, arguably two, are not violations.
2. Window rasterisation (W4) cannot rely on `GUARD_MM = 0.002`: `oracle.raster_guard(p, reach)` gives the inflation
   that makes every cell-to-cell step legal (noted in §4.7). With it the search space near pad corners shrinks by
   that amount; without it the emit-time oracle check will reject such steps and re-search.
3. The exact check has to run when a segment is emitted, not at the end of a pass (§4.8 already says so; the reason
   is now measured, and it applies to on-grid runs, not just joins).
4. Regression test: `tests/test_v2_oracle.py::test_one_air_max_regression` rebuilds the two pads and segments with
   the real numbers: the oracle rejects the GND segment (gap 0.09774 < 0.1) and accepts the +3V3 one, v1's
   `seg_clear` rejects both, and on a 0.15 mm v1 grid all four end cells are free.

Rerun: `bench/route/trace_verify.py <board>` (~2.5 min for one_air_max); the recorded traces are
`bench/route/results/oracle_diff/trace_one_air_max.json` and `trace_coldfire.json`.

### A2. Measured KiCad DRC epsilon and the chosen `TOL_MM`

**Seeded at-the-rule ladder** (`bench/route/oracle_diff.py seeded`, 23 s). A generated board (design rules in its
`.kicad_pro`: min clearance 0.15, default class 0.2, a 0.3 class, a pair class 0.25 with pair gap 0.16, edge 0.3,
hole 0.25, hole-to-hole 0.5) with 34 probe cases; each probe is placed at `need + δ` for
δ ∈ {+6, +2, +1, +0.5 … −0.7 (0.1 steps), −0.9, −1.0, −1.1, −2, −6} µm, one board per δ, judged by
`kicad-cli pcb drc --all-track-errors --refill-zones`.

| Rule (cases) | KiCad accepts down to | KiCad flags from |
|---|---|---|
| `clearance`: track or via against rect / circle / oval / round-rect / rotated / off-grid / THT pads, parallel and 45° tracks, track end caps, via copper | −0.5 µm | −0.6 µm |
| `clearance` with a pad override (0.35, 0.17, and 0.12 floored to the board minimum 0.15) and a footprint override 0.3 | −0.5 µm | −0.6 µm |
| `clearance` to a net of a 0.3 class (max of the two classes) | −0.5 µm | −0.6 µm |
| `clearance` between the two nets of a coupled pair: pair gap 0.16 (track and pad); other nets keep 0.25 | −0.5 µm | −0.6 µm |
| `hole_clearance`: track to NPTH, track to a via's drill, a via's own drill to a foreign track | −0.5 µm | −0.6 µm |
| `hole_to_hole`: via to NPTH, via to same-net THT hole, via to same-net via | −0.5 µm | −0.6 µm |
| `copper_edge_clearance`: track, via (edge drawn 0.1 mm wide: measured to its centre line) | −0.5 µm | −0.6 µm |
| 45° track past a rectangular pad corner | −0.4 µm | −0.5 µm (the 1 nm rounding of the end points lands it at −0.5004) |
| `items_not_allowed`: track in a keep-out | 0.0 | −0.1 µm |
| `items_not_allowed`: via in a keep-out | −0.5 µm | −0.6 µm |
| `solder_mask_bridge`: track to a pad with mask margin 0.3 | 0.0 | −0.1 µm |

So KiCad 10 reports a clearance / hole / edge error **iff the gap is more than 0.5 µm short** (strictly: 0.5004 is
flagged), and has no such epsilon for tracks in keep-outs and for mask bridges.

Oracle against the same boards (714 probe verdicts per tolerance):

| `tol` | false-legal (KiCad flags, oracle accepts) | false-illegal |
|---|---|---|
| 0.0 | 0 | 160 (all within KiCad's epsilon) |
| **0.0003** | **0** | 67 |
| 0.0004 | 0 | 36 |
| 0.0005 | 1 (the 45° corner case at −0.5 µm) | 6 |
| 0.001 (the design's first guess) | 124 | 5 |

**Chosen: `TOL_MM = 0.0003`.** 0.4 µm is the largest tolerance with zero false-legal; 0.1 µm is taken off because
the worker's extract rounds static geometry to 0.1 µm (≤ 0.05 µm in position plus ≤ 0.05 µm in half-size), which
the ladder only probes in one case. The tolerance is applied to clearance / hole / edge; keep-out and mask needs are
judged with none (for a via in a keep-out that is 0.5 µm stricter than KiCad). A 0.5 nm float fuzz is allowed on
top everywhere.

**Corpus references** (`bench/route/oracle_diff.py refs`, 40 s; `tests/test_v2_oracle.py --full`). The human
routing of each board is committed to a scratch copy of the unrouted corpus board (segments and through vias from
`reference.json`; arcs as the polylines stored there) and both judges look at every committed item.

| Board | items | legal / legal | flagged / flagged | false-legal | oracle only |
|---|---|---|---|---|---|
| cm5_minima | 2486 | 2193 | 293 | 0 | 0 |
| coldfire | 3188 | 3182 | 0 | 0 | 6 |
| complex_hierarchy | 364 | 364 | 0 | 0 | 0 |
| ecc83 | 59 | 59 | 0 | 0 | 0 |
| interf_u | 815 | 815 | 0 | 0 | 0 |
| multichannel_mixer | 605 | 593 | 12 | 0 | 0 |
| one_air_max | 2063 | 2033 | 4 | 0 | 26 |
| phone_breathalyzer | 163 | 160 | 0 | 0 | 3 |
| pic_programmer | 376 | 376 | 0 | 0 | 0 |
| sonde_xilinx | 211 | 211 | 0 | 0 | 0 |
| stickhub | 1375 | 1278 | 81 | 0 | 16 |
| tinytapeout | 2538 | 2500 | 9 | 0 | 29 |
| **total (12 boards)** | **14 243** | **13 764** | **399** | **0** | **80** |

`synth_bus_cross` and `synth_qfp_fanout` have no reference routing and are not in this test. The 399 items flagged
by both were flagged for the same rule (396 clearance, 3 shorts — checked item by item on the five boards that have
any). The references contain no hole, edge, keep-out or mask violation on a track, so on real boards only the
clearance / short rules are confirmed in the "must flag" direction; the others rest on the seeded ladder.

The 80 items only the oracle flags, by reason (all on the strict side):

| Count | Reason | Fixable where |
|---|---|---|
| 37 | a graphic on a mask layer is a hard keep-out for every net (v1 policy; KiCad allows one net under an opening) | policy switch `Oracle.mask_openings_hard` |
| 26 | copper graphics (text, lines) are taken as their bounding box | worker extract |
| 13 | gap between 0.3 and 0.5 µm short: inside KiCad's epsilon, outside `TOL_MM` | by design |
| 3 | `phone_breathalyzer`: an unconditional custom `.kicad_dru` clearance (0.127) overrides the 0.2 class in KiCad; `dru_minimums` only ever raises | `model.dru_minimums` (v1 file) |
| 1 | custom pad shape arrives as an outside polygon (5 µm arc error) | worker extract |

What it took to get to zero disagreements, i.e. what the first run got wrong (513 oracle-only items, 0 false-legal):
footprint-level clearance overrides (`tinytapeout` U6 0.18, `cm5_minima` U702 / Module302) and coupled
differential pairs (`cm5_minima` 90 Ω class: 0.154; `tinytapeout` usb: 0.165) — both now modelled.

**Known gaps of the oracle that this test cannot see** (none is exercised by committing reference segments):

- Arcs of copper already on the board reach the model as chords of ≤ 10° (worker `_arc_points`), up to 0.4 % of the
  radius *inside* the true arc — the one approximation that is not on the strict side. It matters only when routing
  next to kept arc tracks (`ripup` other than `all`).
- Zone fills: a foreign pour is not an obstacle (KiCad refills around new copper); whether the refill starves a
  thermal or splits a pour is only known after S7.
- Conditional custom rules, zone-level clearances, blind / buried vias, slots in hole-to-hole.
- Observed while building the ladder, not investigated further: a probe track of net B that overlapped a via of net A
  sitting on a pad of net A was reported by KiCad as `Track [A]` with **no** short — the saved/loaded board carried
  the pad's net on the track. A short onto pad-connected copper may therefore show up as a missing connection rather
  than as a DRC error; the judge (W2) should not rely on a `shorting_items` marker alone.

**Speed** (same runs, pure Python, per call, median): `seg_ok` 16–45 µs, `seg_legal` 13–18 µs, `via_ok` 16–45 µs
(2-layer `interf_u` … 6-layer `cm5_minima`, with the whole reference routing as dynamic copper); `add_piece` 6–11 µs;
`Oracle()` < 10 ms; `local_overrides_from_file` ≤ 0.09 s. The §8 budget (≤ 40 µs median) is met except `seg_ok` /
`via_ok` on the 6-layer board (45 µs).

### A3. Tile capacities against human routing, and the global stage on the corpus (W3)

Rerun: `bench/route/tiles_cal.py cal` and `... global` (results: `bench/route/results/tiles_cal/calibration.json`,
`global.json`), `tests/test_v2_tiles.py --full`, `tests/test_v2_globalroute.py --full`. Twelve boards have a human
reference; the two synthetic boards have none. Capacities are those of §4.6 "as built" (reference class = the board's
default class, `T` by `choose_tile_mm`); demand = the reference tracks crossing each tile border, a crossing inside
the shadow of static copper of the track's own net not counted.

**Calibration at `CAL = 1.0`** ("short" = borders whose demand exceeds the capacity)

| board | T mm | grid | borders | used | short (tracks counted) | ok of all | ok of used | ok of used, by width |
|---|---|---|---|---|---|---|---|---|
| cm5_minima | 1.0 | 62×65×6 | 37 188 | 2 403 | 0 | 1.0000 | 1.0000 | 0.9734 |
| coldfire | 1.0 | 158×92×4 | 115 288 | 8 410 | 1 | 1.0000 | 0.9999 | 0.9869 |
| complex_hierarchy | 1.0 | 101×81×2 | 32 360 | 1 226 | 0 | 1.0000 | 1.0000 | 0.9935 |
| ecc83 | 1.0 | 53×47×2 | 9 764 | 191 | 3 | 0.9997 | 0.9843 | 0.9843 |
| interf_u | 1.0 | 116×109×2 | 48 902 | 4 809 | 30 | 0.9994 | 0.9938 | 0.9757 |
| multichannel_mixer | 1.0 | 110×111×2 | 48 398 | 1 742 | 0 | 1.0000 | 1.0000 | 0.8703 |
| one_air_max | 1.0 | 100×72×4 | 53 812 | 3 519 | 14 | 0.9997 | 0.9960 | 0.9071 |
| phone_breathalyzer | 0.5 | 44×96×2 | 16 616 | 477 | 11 | 0.9993 | 0.9769 | 0.9560 |
| pic_programmer | 1.0 | 161×100×2 | 63 878 | 1 701 | 15 | 0.9998 | 0.9912 | 0.9912 |
| sonde_xilinx | 1.0 | 81×44×2 | 14 006 | 566 | 15 | 0.9989 | 0.9735 | 0.8534 |
| stickhub | 0.5 | 33×80×2 | 9 556 | 941 | 32 | 0.9967 | 0.9660 | 0.8693 |
| tinytapeout | 1.0 | 105×81×4 | 66 868 | 6 229 | 40 | 0.9994 | 0.9936 | 0.9518 |
| **pooled** | | | 516 636 | 32 214 | 161 | **0.99969** | **0.99500** | 0.95663 |

Pooled over `CAL` (share of all borders / of used borders):

| CAL | 0.8 | 0.9 | 1.0 | 1.1 | 1.2 | 1.3 |
|---|---|---|---|---|---|---|
| tracks counted (one reference pitch each) | 0.99754 / 0.96051 | 0.99801 / 0.96815 | 0.99969 / 0.99500 | 0.99969 / 0.99503 | 0.99969 / 0.99510 | 0.99969 / 0.99510 |
| by width (`w + clearance` of the human track) | 0.99472 / 0.91532 | 0.99586 / 0.93366 | 0.99730 / 0.95663 | 0.99766 / 0.96250 | 0.99827 / 0.97219 | 0.99830 / 0.97268 |

Demand / capacity of the 32 214 used borders (tracks counted): ≤ 0.25: 1 882, ≤ 0.5: 18 071, ≤ 0.75: 10 937,
≤ 1.0: 1 163, ≤ 1.25: 3, ≤ 1.5: 1, ≤ 2.0: 26, > 3 or no capacity at all: 131.

What this says:

- The gate of §4.6 ("cap × CAL ≥ observed demand on ≥ 99.5 % of borders") is met at `CAL = 1.0` when tracks are
  counted: comfortably over all borders, by a hair over the borders a track actually crosses (161 short of 32 214 =
  99.5002 %). Raising `CAL` does not help: almost all of the 161 are borders with no capacity at all, which no factor
  repairs. `CAL` stays 1.0 and `CORRIDORS_ADVISORY` is False.
- Weighted by the widths humans actually drew, 4.3 % of the used borders are short: humans route wider than the
  net class on `multichannel_mixer`, `sonde_xilinx` and `stickhub`, and at `T = 0.5 mm` a tile holds 1.25 tracks of a
  0.4 mm pitch, so every fourth dense border sees two. Per board the used-border share is below 99.5 % on seven of
  twelve boards under either reading. The model is a good global picture and a weak local proof; detail keeps its
  wider fallbacks for that reason.
- The 131 used borders without capacity were not inspected one by one. The ones looked at on `interf_u` before the
  corner join were free gaps straddling a tile corner; the join removed 14 of 43 there.
- Before slack and the corner join (the design's formula alone) the same measurement gave 98.50 % of all borders and
  75.96 % of used borders by width at `CAL = 1.0`, and 99.55 % / 92.70 % at 1.2.
- Tile graph build: 0.03–0.16 s per board (budget 3 s).

**Global stage on the corpus** (connections of v1's planner for signal nets, pour stubs excluded; default plan;
budget = the stage's 10 % share of the default budget, 1.8 M units; one unit per tile expansion)

| board | conns | T mm | nodes | tiles s | global s | work units | iterations | overflow first → kept mm | cuts | planned vias |
|---|---|---|---|---|---|---|---|---|---|---|
| cm5_minima | 156 | 1.0 | 24 180 | 0.06 | 0.52 | 193 445 | 11 | 125.8 → 0 | 0 | 152 |
| coldfire | 307 | 1.0 | 58 144 | 0.14 | 3.46 | 1 140 338 | 17 | 443.0 → 0 | 0 | 398 |
| complex_hierarchy | 87 | 1.0 | 16 362 | 0.07 | 0.12 | 18 138 | 8 | 29.2 → 0 | 0 | 4 |
| ecc83 | 14 | 1.0 | 4 982 | 0.03 | 0.01 | 328 | 2 | 4.2 → 0 | 0 | 0 |
| interf_u | 159 | 1.0 | 25 288 | 0.08 | 1.59 | 482 547 | 22 | 540.1 → 0 | 0 | 101 |
| multichannel_mixer | 162 | 1.0 | 24 420 | 0.08 | 0.21 | 41 886 | 8 | 34.8 → 0 | 0 | 26 |
| one_air_max | 214 | 1.0 | 28 800 | 0.08 | 0.47 | 81 860 | 15 | 67.3 → 0 | 0 | 99 |
| phone_breathalyzer | 43 | 0.5 | 8 448 | 0.03 | 0.09 | 23 085 | 13 | 20.2 → 0 | 0 | 13 |
| pic_programmer | 86 | 1.0 | 32 200 | 0.10 | 0.23 | 40 208 | 9 | 44.3 → 0 | 0 | 10 |
| sonde_xilinx | 44 | 1.0 | 7 128 | 0.04 | 0.06 | 13 172 | 8 | 35.3 → 0 | 0 | 5 |
| stickhub | 99 | 0.5 | 5 280 | 0.03 | 0.43 | 172 741 | 30 | 21.1 → 0.3 | 1 | 33 |
| synth_bus_cross | 24 | 1.0 | 2 800 | 0.01 | 0.23 | 85 851 | 8 | 158.0 → 0 | 0 | 46 |
| synth_qfp_fanout | 60 | 0.5 | 20 000 | 0.05 | 0.94 | 293 141 | 12 | 259.2 → 0 | 0 | 47 |
| tinytapeout | 349 | 1.0 | 34 020 | 0.13 | 5.49 | 1 813 611 | 14 (budget) | 533.5 → 4.3 | 23 | 513 |

- Twelve boards reach zero overflow (borders, via columns and forced crossings); `stickhub` keeps one crossing after
  30 iterations; `tinytapeout` runs out of the 1.8 M units after 14 iterations with 4.3 mm on 23 cuts.
- Same twice: all 14 boards give identical corridors on a second run (`tests/test_v2_globalroute.py --full`).
- Throughput 210–330 k expansions/s on the boards above 40 k expansions, on a machine that was running other
  benchmark jobs at the same time; the §8 budgets (≤ 60 000 nodes, ≤ 10 s, ≤ 30 iterations) held in every run made.
- History of the model on this table: with the design's formula alone five boards kept 24–51 mm of false overflow
  after 30 iterations; with slack every board converged but planned 0 vias for `synth_bus_cross` and 97 for
  `coldfire`; the crossing model produced the via counts above.
- With S0's connections (roles, widths, pour stubs as open terminals) instead of v1's planner the picture is the
  same except `interf_u`: 30 iterations, 1.5 mm left on 10 borders.

Not measured: whether detail routing succeeds more often inside these corridors than without them (that is the
stage-3 gate, a matter for the benchmark once W4 and W7 have settled), and the `only=` feedback path beyond two
timing probes (12 connections: 0.7 s on `interf_u`, 1.0 s on `tinytapeout`).
A4. Reconciliation of §6.1 against the existing plan field names.
