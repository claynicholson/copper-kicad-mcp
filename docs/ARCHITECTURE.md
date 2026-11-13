# Copper for KiCad — architecture & module contracts

Copper is a stdlib-only (+ Pillow) Python MCP server. It runs on any Python
≥ 3.10, including KiCad's own bundled interpreter. The installer uses `uv` to
put it in an isolated environment and registers it with every AI client it
finds.

## Neutrality: no model, no client, no vendor is assumed

* **Model / client.** The server never calls a language model. It speaks plain MCP over stdio, so any client and
  any model can drive it; `copper config` prints a stdio config for clients the installer does not know. Tool
  descriptions, server instructions, prompts and the guide say "the agent" / "your AI client", never a product.
  Product names appear only where they are the subject: `install/clients.py` (registering with a specific app),
  the plugin folder, and the install sections of the README.
* **`_meta`.** `tools/list` marks a handful of core tools with `"_meta": {"anthropic/alwaysLoad": true}`. `_meta`
  is the protocol's reserved extension field and its keys are namespaced; a client that does not know a key
  ignores it, so the hint is harmless elsewhere. It only tells clients that defer tool loading which tools to
  load up front.
* **Parts vendor.** Tools talk to the provider interface in `parts/providers/`; LCSC/JLCPCB is the default
  provider, not an assumption ([PROVIDERS.md](PROVIDERS.md)).
* **Fab.** Design rules, file formats and order gates come from a fab profile, a JSON file in `data/fabs/` or the
  user's own ([FAB_PROFILES.md](FAB_PROFILES.md)).
* **Settings.** One layered loader, `config.py`: defaults < `$COPPER_HOME/config.json` <
  `<project>/.copper/config.json` < call arguments ([CONFIG.md](CONFIG.md)).

`tests/test_neutral.py` enforces all of this: vendor names may appear only in the files that implement the default
provider / profile, and product names only in the install files.

## Channels

| Channel | Used for |
|---|---|
| `kicad-cli` (`kicad.env.run`) | ERC, DRC, netlist, BOM, all exports, renders, `sch upgrade --force` (canonical re-format after edits) |
| S-expression engine (`sexpr`) | All `.kicad_sch` edits; lib tables; `.kicad_pro` |
| KiCad's Python + `pcbnew` (`kicad.env.kicad_python()`) | PCB edits on KiCad 10 (the active install's own Python; see COMPATIBILITY.md). Runs as a short-lived subprocess **worker script**. Never imported into the server process |
| IPC (`kipy`, optional `live` extra) | Live PCB operations when KiCad is running with the API enabled. Optional and capability-gated |

## Package layout (`src/copper_kicad/`)

```
__init__.py        version + brand constants
cli.py             `copper` CLI: serve | setup | install | uninstall | doctor | status | render | version
sexpr.py           lossless S-expression loads/dumps + helpers (find, find_all, value, set_value, Sym)
svgraster.py       rasterize(svg, png, width, background) — KiCad SVG → PNG with Pillow
render.py          schematic(sch, out_dir, ...) -> [Image]; pcb_layers(pcb, out_dir, layers, ...) -> Image; pcb_3d(pcb, out_dir, view) -> Image
project.py         Project(dir,name){pro,sch,pcb,copper_dir,out_dir,sheets()}; resolve(project=None); guard_writable(path, while_open);
                   atomic_write(path, text, base_revision); revision(path); checkpoint(pr,label); list_checkpoints; restore_checkpoint
resources.py       MCP resources + prompts: dispatch(method, params)
mcp/registry.py    @tool decorator, Tool, Result(data, images, summary), Image(path|data), ToolError(message, fix, code)
mcp/server.py      stdio JSON-RPC server (threads, progress, cancellation)
kicad/env.py       installs(), kicad() -> Install{cli, python, share, major, full_version}, config_dir(), path_vars(project_dir),
                   expand(uri, vars), lib_tables('sym'|'fp', project_dir) -> [Lib{name,type,uri,path,scope,exists}],
                   find_lib(kind, name, project_dir), run(args, timeout, cwd, check) -> CompletedProcess, kicad_python(), summary()
kicad/libs.py      symbol/footprint library index + search + read (symbol defs incl. `extends`), footprint read
sch/               schematic engine (model, symbols, ops, layout, netlist)
config.py          layered settings: parts.providers, parts.policy, fab.profile (defaults < user < project < call)
parts/providers/   the provider interface (base.Provider) + registry (entry points, config paths) and the built-ins:
                   lcsc (wraps the modules below), local (catalogue file), kicad (generic library parts)
parts/             the default provider's engine: LCSC/JLCPCB search, detail, EasyEDA → KiCad import, basic-parts
                   table, BOM sourcing (source_bom = LCSC pass, source_bom_generic = any provider list)
pcb/               board sync / setup / placement / routing / zones (pcbnew worker scripts in pcb/workers/)
review/            verify (ERC/DRC/parity), design review heuristics, fab_profiles (profile loader),
                   fab (fab_export: the order pack for a profile), exports
install/           client registration (Claude Desktop/Code, Codex, Cursor, VS Code, Windsurf, Zed, Gemini), doctor
tools/             MCP tool definitions — one module per area, imported by tools/__init__.py
skills/            SKILL.md files (plain markdown agent skills; shipped as a plugin to the clients that support them)
host/              the live host (the Copper app that owns the open document): client, rendezvous, loopback relay,
                   and live.py = shadow checkout -> engine -> uuid patch -> host.apply (HOST_PROTOCOL.md)
sch/patch.py       uuid diff of a sheet before/after an edit, and its application to file text
                   (the reference applier); sch/model.live_mode = an edit for a live host: no kicad-cli re-save, no ERC.
                   host/route.py + project.guard_writable: a write to a project with a live host is routed there
agent/             copper-agent, the app's sidecar (`python -m copper_kicad.agent`): stdio JSON-RPC, turns, events,
                   conversations, model drivers, no-model commands. The ONLY package that may run a model loop;
                   nothing outside it imports it, and the MCP server above still never calls a model
data/              bundled data: fabs/*.json (fab profiles), templates, and the default provider/profile's own
                   basic_parts.json.gz (LCSC/JLC seed) and jlc_rotations.json (JLCPCB placement corrections)
```

## Cross-module contracts (do not change signatures without updating callers)

```python
# sch/netlist.py
def export_netlist(pr: Project) -> dict:
    """{'components': {ref: {'value','footprint','lib_id','fields':{..},'sheet'}},
        'nets': {net_name: [{'ref','pin','pin_name','pin_type'}]}}  — from `kicad-cli sch export netlist --format kicadsexpr`"""

# sch/ops.py
def apply_ops(pr: Project, ops: list[dict], dry_run=False, while_open='refuse') -> dict:
    """Applies ops atomically to the root sheet (or op['sheet']). Returns
    {'changed': [...], 'refs': {temp_key: ref}, 'warnings': [...], 'revision': str}"""

# parts/providers/__init__.py
def active(cfg, provider=None) -> list[Provider]          # the providers a call asks, in order
def resolve_part(pr, part_id, provider=None) -> dict       # {'lib_id','footprint','value','fields','provider','part_field'}
def resolve_spec(pr, spec, provider=None) -> (dict | None, warnings, tried)
def part_field(cfg, providers=None) -> str | None          # the project's supplier part-number field
def part_number(fields, field) -> str                      # that field's value on one component

# review/fab_profiles.py
def active(pr=None, name=None) -> dict                     # the complete, validated profile
def rules(profile, tier='standard', layers=None) -> dict   # one rule set in pcb/rules.py's shape

# review/fab.py
def fab_export(pr, profile=None, out_dir=None, waive=None, layers=None, assembly=None) -> dict

# parts/importer.py  (lcsc provider)
def resolve_lcsc(pr: Project, lcsc: str) -> dict:
    """C-number → {'lcsc','lib_id','footprint','value','fields':{LCSC,MPN,Manufacturer,Datasheet,Description},
    'basic': bool, 'source': 'kicad-stock'|'easyeda'}. Imports symbol/footprint/3D into the project's
    `copper-lcsc` libs only when no stock KiCad symbol+footprint fits."""

# parts/basic.py
def pick(spec: str) -> dict | None:
    """'10k 0603' / '100nF 0402 16V' / 'LED red 0603' → best JLC Basic part {'lcsc','value','package','description'} (offline)."""

# review/verify.py
def run_erc(pr: Project) -> dict   # {'errors': int, 'warnings': int, 'violations': [finding], 'report': path}
def run_drc(pr: Project, refill=True, parity=True) -> dict
def erc_brief(pr: Project, limit=12) -> dict   # counts + top findings, cheap enough to run after every mutation
# finding = {'severity','type','message','items':[...],'at':[x,y] (mm),'fix': '<tool>(<args>) or instruction'}
```

### `sch_apply` op schema

Pin references are `REF.PIN`, where PIN is a pin number or a pin name
(`U1.PA9`, `U1.12`, `J1.VBUS`, `R1.2`). Coordinates are in mm on a 1.27 mm (50 mil) grid. Copper snaps everything to that grid.

| op | fields |
|---|---|
| `place` | `lib_id` *or* `lcsc` *or* `part` (+ `provider`) *or* `spec` ("10k 0603"); optional `ref`, `value`, `footprint`, `at:[x,y]`, `rot` 0/90/180/270, `mirror` x/y, `unit` (int or "all"), `fields:{}`, `group` (layout hint: mcu/power/connector/passive/…), `near` (ref to place beside), `dnp` |
| `connect` | `pin` or `pins:[...]`, `net`. Places a pin-anchored label stub. Power nets (GND, +3V3, VBUS, …) automatically become power symbols |
| `power` | `pin`, `net` — forces a power symbol |
| `no_connect` | `pin` or `pins` |
| `wire` | `from`/`to` pin refs, or `points:[[x,y],…]` |
| `label` | `net`, `at`, `kind` local/global/hierarchical, `rot` |
| `set_field` | `ref`, `field`, `value` (Value, Footprint, LCSC, MPN, Datasheet, any custom) |
| `move` | `ref`, `at`, `rot`, `mirror` — labels and wires attached to its pins follow |
| `delete` | `ref` (and the labels attached to it) or `label` net name at a pin |
| `disconnect` | `pin` — removes the label, wire or power symbol attached to that pin |
| `swap` | `ref`, `lib_id` / `lcsc` / `part` and/or `footprint` |
| `text` | `text`, `at`, `size` |

## Envelope

Every mutating tool returns:

```
{changed:[...], revision, verify:{erc:{errors,warnings,new,fixed}, ...}, findings:[{severity,code,message,fix}], preview?}
```

`preview` is an image. Failures raise `ToolError(message, fix=…, code=…)`.

## Wire envelope and credit efficiency

Every byte of a result is re-read by the agent on each later turn, and every round trip re-reads the whole
conversation, so results are shaped once more as they leave the server. Tools still build the full payload and
`registry.call_tool` still returns it (in-process callers and tests read `structuredContent`); `Server._call` runs

```
compact.prepare_args  ->  registry.call_tool  ->  compact.to_wire  ->  ledger.record  ->  reply
```

| Module | Role |
|---|---|
| `mcp/compact.py` | `prepare_args`: image policy and digest defaults for arguments the call did not pass. `to_wire`: project-relative paths, derivable keys and empties dropped (zeros and booleans are KEPT), change digests, grouped findings, one text channel (`summary` + compact JSON; `structuredContent` only with `COPPER_STRUCTURED=1`), a size cap with spill to `<project>/.copper/results/`. |
| `mcp/ledger.py` | Per-session credit meter: calls, errors, seconds, argument bytes, result bytes, image pixels per tool; estimated tokens (bytes / 4, pixels / 750). Counters only, flushed to `$COPPER_HOME/sessions/`. Rows are registered tool names only (a made-up name is counted under `(unknown tool)`). Shown by `copper_status` (`session`), `copper_status(detail='credits')`, the resource `copper://session/credits` and `copper credits [--last N]`. |
| `mcp/catalogue.py` | What `tools/list` says: one short text for shared parameters, lean annotations, slim descriptions (`SLIM`). A slim text is tied to the tool's argument names; when the owner changes them the owner's full description is served again and `catalogue.stale()` lists the tool. The full text always stays on the help page. |
| `tools/flow_tools.py` | `board_build`: sync, outline, placement, routing, pour and DRC in one call with one bounded report. NEW boards get the full flow, except that parts already placed inside the outline without overlaps stay where they are; on a board that has copper nothing existing is moved, ripped up or redrawn, and footprints are swapped to the schematic's only if board_build built the board (`built_here` in `.copper/build.json`), otherwise the differences are reported. Fix hints in the report are never cut inside a call. Time-boxed (`pending` + resume through `.copper/build.json`). |

Rules for tool authors:

* Never rely on a key being dropped or kept by the compactor; build honest payloads. Never dropped: `fix`, `next`,
  `message`, `error`, `ok`, `fab_ready`, any number, any boolean outside `compact.NOISE_FALSE`.
* `Result(raw=True)` bypasses compaction; `@tool(max_bytes=…)` raises the cap for a detail view; a call that passes
  `limit`, `what`, `refs`, `query`, `net` or `detail` gets four times the cap.
* Pictures: single-step tools attach none unless the caller passes `preview` / `render` = true
  (`COPPER_IMAGES=auto` restores the tools' own defaults, `never` also silences `board_build`). `render` defaults to
  the smallest legible width; no image leaves above 1400 px unless `width` was passed. A drawing over 300 kB of base64 is re-encoded with a
  256-colour palette (same pixels and tokens, about a third of the bytes; 3D views excepted). `pcb_read(refs=...)`
  and `pcb_read(net=...)` without `what` return only those footprints / that net.
* Long examples and edge cases go in `help=`; a description says what the tool is for, its argument shapes and one
  example. `tests/test_tokens.py` fails when `tools/list` outgrows its budget.

Environment knobs: `COPPER_IMAGES` (never | final | auto), `COPPER_MAX_RESULT_BYTES` (default 6000, 0 = no cap),
`COPPER_STRUCTURED=1`, `COPPER_COMPACT=0` (raw results, for debugging and before/after measurements),
`COPPER_CATALOGUE=full` (owners' full descriptions), `COPPER_LEDGER=0` (no session file),
`COPPER_CALL_BUDGET_S` (board_build time box).

## Schematic layout engine (`sch/pro/`)

`engine.relayout(pr, sheet, ...)` is the only entry point (`sch_tidy`, and `sch_apply` through `tool.after_apply`).

| Module | Role |
|---|---|
| `gate.py` | Identity gate. `baseline()` stages the project (relative paths kept) and exports netlist + ERC; `check()` re-saves the candidate with `kicad-cli sch upgrade`, then G0 symbol payload, G1 `canon()` of the root netlist, G2 interface labels / PWR_FLAGs / no-connects, G3 name inventory per net (equality), G4 ERC as a keyed multiset. `GateResult.text` is what gets written. |
| `blocks.py` | `sheet_view()` (what each net is called ON THIS SHEET: labels, power symbols, flags) and `Design` (anchors, small parts, roles, blocks with kind / rank / title). |
| `build.py` | `Blk.build()`: anchor with stems (up / down / series), rail groups, decoupling banks, and docked cells for everything else; `strip_cell()` for PWR_FLAGs and second names. Constructive: nothing is emitted that overlaps what is already there. |
| `route.py` | Bounded A* for wires inside a block (window 20 cells, 40 000 expansions, named nets only up to 26 cells apart, never IC to IC); `verify()` = geometric pre-check. |
| `emit.py` | Unit-edge wire union, pruning, junctions; `LabelPool` (labels are moved); frames as `copper:block:*` groups nested in `copper:layout:<digest>`; deterministic uuids; title block. |
| `score.py` | `measure()`: readability metrics. A ratchet for fixtures and a tie-breaker between candidates of one sheet, not a judgement of hand-drawn sheets. |

### Style `pro2`

`sch_tidy(style='pro2')` runs the redesigned engine; the design, its rules and its work packages are in
[SCHEMATIC_PRO_V2_DESIGN.md](internal/SCHEMATIC_PRO_V2_DESIGN.md). The default style is still `pro`. Ladder:
`pro2` (+ symfix candidate) -> `pro2-safe` -> `pro` -> `pro-plain` -> `labels`, at most three candidates gated.

| Module | Role |
|---|---|
| `idioms/` | `Match` / `Template` and the matchers: passive idioms (`passive.py`) and stage idioms (`stage.py`: LDO, boost / buck line, op-amp loop, crystal, USB-C CC). Every template wire is asserted against `pin_net`. |
| `dock.py` | Per-block docking of idiom cells on the pin rows of the anchor IC. |
| `flow.py`, `page.py` | Block flow graph and ranks; rank columns on a 2.54 mm grid, paper choice, reserved title-block and notes rectangles. |
| `route.py` | Also `verify_partition()`: the pure-Python union-find pre-gate that runs before any kicad-cli call. |
| `text.py` | Candidate solver for Reference / Value texts on the finished drawing: re-seats only what touches something, greedy most-constrained first, one grow ring. |
| `notes.py` | Deterministic design notes from the netlist (rails and sources, FB-divider Vout, LED current, RC corner, crystal load, I2C pull-ups, connector names, decap placement) and the title-block summary. Text items owned by the `copper:block:*` groups. |
| `symfix.py` | Pin texts that overprint. Level 1: detect + hint. Level 2 (`symfix='auto'`): the sheet's embedded copy of the symbol is redrawn (stacked pins fanned, pin-name offset widened); same key, same pins - only for Copper-generated (`copper-lcsc`) symbols; a KiCad stock or user library symbol is never redrawn (owner rule "default symbols first"), it gets the hint. |

The gate has exactly one relaxation, used only for the symfix candidate: `gate.check(symfix=True)` lets a changed
embedded library symbol through G0 when `symfix.equivalent()` finds every pin and property equal, and then does
NOT exempt the library-symbol ERC checks for the parts that use it. A `lib_name` redirect is always refused
(KiCad writes it into the netlist; `bench/sch/symfix_probe.py --lib-name` shows G1 failing).
Tools around it: `bench/sch/run.py` (corpus + unseen designs, independent kicad-cli witness, `acceptance.json`),
`bench/sch/textcheck.py` (`model.text_width` against KiCad's rendering).
| `engine.py` | Candidates `pro` -> `pro-plain` (no router) -> `labels` (the older `ops.tidy`, run on a staged copy); the first that passes pre-check and gate is written. `sheet_state()`: `empty` / `engine` (digest matches) / `machine` / `hand`. |

Rules that must hold: the engine never names a net (labels and power symbols are re-issued from the sheet's own);
a net without a name on the sheet must end up in one wired piece or the candidate is dropped; reference
designators are never changed; no step calls anything but the local `kicad-cli`.

