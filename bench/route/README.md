# Routing benchmark

Objective scoring of a PCB autorouter on real, human-routed KiCad boards (plus two synthetic stress cases).
KiCad itself is the judge: completion and DRC come from `kicad-cli pcb drc` on the routed board, length /
vias / widths from pcbnew. Nothing a router reports about itself is trusted for the score.

```
PY="C:/Program Files/KiCad/10.0/bin/python.exe"            # KiCad's Python (has Pillow)
$PY bench/route/prepare.py [--force] [--only NAME]         # (re)build corpus/ from the KiCad demos — already done
$PY bench/route/run.py --router guided --list              # the corpus
$PY bench/route/run.py --router guided                     # all boards, 2 runs each (determinism)
$PY bench/route/run.py --router guided --board stickhub    # one board
$PY bench/route/run.py --router freerouting --runs 1       # Freerouting (needs the Java + jar Copper downloaded)
$PY bench/route/run.py --router v2 --name v2-try3 --opt plan='{"global":{"via_cost":8}}'
$PY bench/route/run.py --router guided --summarize         # rebuild results/guided.md from results/guided/*.json
$PY bench/route/run.py --router v2 --suite cons --name cons_A      # the constraint boards (length groups, pairs, keep-short)
$PY bench/route/run.py --router v2 --suite cons --name cons_A_off --opt plan='{"global":{"constraints":"off"}}'
#   (results/constraints_baseline.md = before any tuning; results/constraints_tune.md = length tuning, before / after per group;
#    results/constraints_final.md = coupled pairs, skew, widths, keep-short: the four boards, the gate and the 14-board corpus;
#    results/constraints_fix1.md = after the independent verification: the six failures before / after, suite, gate, corpus;
#    results/constraints_fix2.md = after the second verification: the two failures before / after, suite, gate, corpus)
$PY bench/route/diagnose.py guided [board ...]             # failure-mode digest
$PY bench/route/steer_demo.py coldfire --rule order --base v2_stage7   # one plan revision from the router's own suggest[], before / after
```

Boards can be run in parallel in separate processes (one `--board` each); every process rewrites the
summary from the per-board files, so finish with `--summarize`.

`--suite route` (the default) is the 14-board corpus: its table and totals are comparable from run to run.
`--suite cons` (alias `--group constraints`) is the four constraint boards, `--suite all` both; a `--board` named
explicitly runs whatever its suite. Constraint boards get their own section in `results/<name>.md` and never count
in the corpus totals. When `results/<name>_off/` exists (the same boards with `global.constraints: "off"`), it
supplies the wall-time limit of the verdict.

## Files

| | |
|---|---|
| `corpus/<board>/<board>.kicad_pcb` | UNROUTED copy (tracks + vias + teardrops removed, zones refilled, footprints and rules kept) |
| `corpus/<board>/<board>.kicad_pro`, `.kicad_dru` | the original's net classes / design rules |
| `corpus/<board>/meta.json` | facts (layers, pads, pitch, per-net MST), the human reference (total + per-net length, vias, widths, layers, DRC of the original) and the DRC of the unrouted board |
| `corpus/<board>/reference.json` | the human routing as segments + vias (for the reference picture) |
| `results/<name>/<board>.json` | every measured number of every run + the router's own report |
| `results/<name>/<board>.png`, `.human.png` | the routed result (yellow = still unconnected) and the reference |
| `results/<name>.md`, `.json` | the table |
| `routers/<name>.py` | router adapters (`guided`, `freerouting`, `none`); see `routers/__init__.py` for the 3-function interface |
| `corpus/cons_*/` | constraint boards: synthetic, with net classes in `.kicad_pro`, KiCad custom rules in `.kicad_dru` (skew, pair gap, uncoupled length, max length) and the expected constraint set + thresholds in `meta.json` (`"suite": "cons"`, `"constraints"`) |
| `cons.py` | constraint metrics and the pass / fail verdict (design §14.11); `common.kicad_lengths()` asks KiCad for every net's length |
| `cons_probe.py` | what KiCad's own DRC says about length / skew / pair rules on tiny hand-made boards (the facts the design rests on) |
| `.work/` | scratch: run copies, a private KiCad config dir, a private COPPER_HOME, temp files (git-ignored) |

## What is measured

* **done %** = (connections on the unrouted board − KiCad's unconnected count after routing) / connections.
  Nets a router decides to skip (e.g. GND "left for a pour") count as not done.
* **DRC err (new)** = all kicad-cli errors with zones refilled; `new` = not on the unrouted board (multiset
  difference on type + text + items), i.e. caused by the routing.
* **len ×** = total track length / the reference's. **common ×** = the same over nets that are complete in
  both and carry tracks in both (like for like; pours make whole-board totals misleading).
* per result JSON also: per-net worst detours, extra vias, nets routed thinner than the reference's
  dominant width, length / MST, share of segments that are not on a 45° direction, share shorter than 0.2 mm.
* **same twice** = two routes of two fresh copies give item-for-item identical copper (1 µm).
* **wall s** = the router call only (not the harness's own measure + DRC).

## Constraint boards (suite `cons`)

| board | what a router has to get right | KiCad rules in its `.kicad_dru` |
|---|---|---|
| `cons_usb_pair` | a USB pair, connector to MCU with a 90 degree turn; class gap 0.15 mm, below the 0.2 mm clearance | skew <= 0.15; pair gap 0.15 .. 0.9; uncoupled <= 6 |
| `cons_bus_match` | SD bus, 4 data + clock, natural lengths about 8 mm apart | skew <= 0.5 over the five nets |
| `cons_pairs_cross` | two pairs that must cross on SMD pads (connectors at the board edge) | per pair: skew <= 0.2; gap 0.15 .. 1.2; uncoupled <= 8 |
| `cons_mixed` | 4 layers: QSPI group, USB pair, crystal, 12 plain nets, plane on In1 | QSPI skew <= 0.3; the USB rules; crystal legs <= 8 mm |

The rules are skew / pair / max-length rules on purpose: KiCad leaves those silent on a net without tracks, so the
unrouted boards carry no constraint error (`prepare.py` refuses to build one that does).

Measured per board, from the routed file and KiCad alone:

* **KiCad lengths** of every constrained net: a copy of the routed board gets one rule no net can meet
  (`length (min 100000mm)`); the "actual" in each DRC message is KiCad's figure (tracks, in-pad and in-via
  shortcut, via height, pad-to-die).
* **group spread** = longest - shortest member against the tolerance; **pair skew**; **vias P / N**;
  **coupled %** = share of the longer net's track length with a parallel segment of the other net on the same
  layer at the pair's gap +- 0.02 mm; **keep-short** length against its maximum.
* **new DRC** includes KiCad's five constraint error types; they are also listed by type.
* **len model err** = largest |router's own length figure - KiCad's| (the one number that reads the router's report);
  **detection** = what the router finds on the unrouted board with no plan, against `meta.constraints`.

A board passes only when every threshold holds (done 100 %, 0 new DRC, same twice, detection exact, len model err
<= 0.005 mm, spread <= tol, skew <= rule, coupled % >= the board's minimum, vias P = N, wall <= 1.5 x the `off`
run + 10 s). A miss is reported with its amount; the rule files are not loosened.

## Adding a router

Create `routers/v2.py` with `NAME`, `available()` and `route(project_dir, pcb, options) -> dict`. It gets a
private copy of the unrouted board and must leave the routed board in the same file. Then
`run.py --router v2`. The harness sandboxes `KICAD_CONFIG_HOME`, `COPPER_HOME` and the temp dir under
`.work/` before the router is imported.

## Caveats

* The reference for `phone_breathalyzer` is whatever routing is on that board today, not a hand layout.
* Humans often rely on pours for power; the unrouted copies keep the zones, so a router has the same pours.
* `one_air_max`, `tinytapeout` and `cm5_minima` carry DRC errors on the UNROUTED board (footprint-level:
  see `meta.json`), which is why `new` is the column to watch.
* The corpus is ~16 MB of board files regenerated by `prepare.py` from a KiCad 10 install.
