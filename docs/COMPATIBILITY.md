# KiCad compatibility

Which KiCad versions Copper works with, how it decides which installed KiCad to use, and what it does so that a
KiCad update (or a second KiCad on the same machine) never damages a design. The code is in
`src/copper_kicad/kicad/compat.py` (rules) and `src/copper_kicad/kicad/compat_report.py` (the `copper doctor` table).

Status of this page: written 2026-11-09 against **KiCad 10.0.6**, the only KiCad binary the behaviour below was run
on. Every statement about another version carries one of these marks:

| Mark | Meaning |
|---|---|
| **verified** | run against the real KiCad 10.0.6 binary by `tests/test_compat.py` |
| **modelled** | covered by a test that uses `tests/data/fake_kicad_cli.py`, a stand-in built from the real 10.0.6 command tree plus KiCad's release notes and CLI documentation. Proves Copper's logic, not that KiCad version's behaviour |
| **unverified** | read from release notes, documentation or source only; no test |

## Supported versions

| KiCad | Status | What works | Evidence |
|---|---|---|---|
| 10.0.6 | supported | everything | verified |
| 10.0.7, 10.0.8 (latest stable, 2026-10-30) | supported, recommended | everything; same file formats and the same kicad-cli command tree as 10.0.6 according to the release notes | **modelled, unverified on a real binary** |
| 10.0.x release candidates (`-rc1`) | treated as pre-releases: used only when pinned or when it is the only install; same file format as 10.0, so writes are allowed | selection modelled | unverified on a real binary |
| 9.0.x | view and verify only | renders, ERC, DRC (without the zone refill), exports that exist in 9. Every edit answers `KICAD_TOO_OLD`. Copper never writes KiCad 9 files | **modelled from the 9.0 CLI documentation, unverified on a real binary** |
| 10.99 nightlies (KiCad 11 development) | never chosen by default | used only when pinned or when the project is already in its format; reads work through kicad-cli, PCB edits answer `PCB_ENGINE_UNAVAILABLE` (KiCad 11 has no pcbnew Python module) | **modelled, unverified on a real binary** |
| 11.0 (planned for early 2027) | not supported for PCB editing yet | as for nightlies. Keep KiCad 10.0.x installed next to it: Copper prefers the stable install that can edit boards | **modelled, unverified** |

`copper doctor` prints this for the machine it runs on, one row per installed KiCad:

```
KiCad                  10.0.6  stable  ACTIVE (newest stable)  sch 20260306  pcb 20260206  engine swig  at C:\Program Files\KiCad\10.0
KiCad up to date       10.0.6 installed, 10.0.8 available — zone fills can short through-hole pins and other-net pads; fixed in KiCad 10.0.7
```

`copper doctor --json` carries the full table under `kicad.installs[*]`: `version`, `series`, `channel`
(`stable`, `rc`, `nightly`, `old`), `active`, `reason`, `formats`, `engine`, `caps` and `advisories`. The MCP tool
`copper_status` returns only the active install with its `reason` and `advisories`, so its size does not grow with
the number of installs.

## Which KiCad Copper uses

`env.kicad()` asks `compat.select()` every time; the answer is cached per project format.

1. `COPPER_KICAD_CLI` (a path) or `COPPER_KICAD_VERSION` (a version prefix such as `10.0`, or `nightly`) wins.
2. Otherwise the newest **stable** KiCad 10 or later that can read every file of the open project, preferring an
   install that can edit boards.
3. If no stable install can read the project because it is in a pre-release format, a pre-release that can.
4. With no project open: the newest stable KiCad 10 or later.
5. If only a KiCad older than 10 is installed it is used in view/verify mode; if only a pre-release is installed
   it is used, and the write guard below still applies.
6. `COPPER_KICAD_ALLOW_PRERELEASE=1` lets pre-releases compete in steps 2 and 4.

A nightly installed next to a stable release therefore never becomes active by itself (it used to, because
`10.99 > 10.0`). The Python that runs board edits always belongs to the active install; the worker also compares
`pcbnew.Version()` with the active kicad-cli before it loads a board and refuses with `ENGINE_MISMATCH` when they
differ (on Linux every KiCad shares the system `python3`, so a path check cannot tell).
Verified on 10.0.6 with a simulated second install; the Linux case is unverified (no Linux machine).

Two projects opened in one session can be served by two different KiCads. The `reason` field in `copper_status`
and `copper doctor` is where that shows.

## File formats

| File | KiCad 9.0 | KiCad 10.0.x | Evidence |
|---|---|---|---|
| `.kicad_sch` | 20250114 | 20260306 | 10.0: verified (measured by running `sch upgrade`); 9.0: unverified |
| `.kicad_pcb`, `.kicad_mod` | 20241229 | 20260206 | 10.0: verified; 9.0: verified for reading (KiCad's bundled demos are in this format) |
| `.kicad_sym` | 20241209 | 20251024 | 10.0: verified |

For a series that is not in this table Copper does not guess: it runs `sch upgrade`, `pcb upgrade` and
`sym upgrade` once on three one-line files in a temporary folder and reads the stamps the binary wrote (cached
with the capability probe). Verified on 10.0.6 (0.7 s once); for other versions modelled.

Rules, all enforced in one place each (`project.resolve`, `project.guard_writable`, `env.run`, `pcb/worker.run`):

* **A file newer than the active KiCad reads is never opened, checked or saved.** Every tool answers
  `FORMAT_TOO_NEW`, naming the file, the KiCad that saved it and the newest format the active KiCad reads.
  The whole project is checked, not only the file a command names: kicad-cli 10.0.6 silently leaves a too-new
  sub-sheet out of the netlist and reports "0 violations" for it. `copper_status`, `project_open` and `checkpoint`
  still work on such a project and report the problem, so an older state can be restored. Verified.
* **A design saved by an older KiCad** (KiCad 10's own demos are in KiCad 9 format) opens as it is. The first
  edit saves the edited file in the active KiCad's format, which is what KiCad itself does on save. Copper takes a
  checkpoint first and reports it once per file as `format_upgraded` with the checkpoint id; that checkpoint
  restores the original bytes. Sheets that were not edited keep their format. The exported netlist is identical
  before and after. Verified on the `complex_hierarchy` demo.
  The upgrade is reported and recorded only once the file on disk really carries the newer stamp: a dry run, a
  failing batch or a file open in KiCad reports nothing. Checkpoints include sub-sheets kept in sub-folders of the
  project (`sch/power.kicad_sch`) and restore them; a sheet outside the project folder cannot be part of a
  checkpoint, so its original is copied to `.copper/format-originals/` and the note names that copy. If neither a
  checkpoint nor a copy can be made, the edit is refused. Verified on the `royalblue54L_feather` demo.
* **A kicad-cli that does not run** (no answer to `kicad-cli version`, for example `COPPER_KICAD_CLI` pointing
  at the wrong file) is refused with `KICAD_CLI_BROKEN` before any file is written; `copper_status` reports it.
* **A pre-release never upgrades a stable-format file on its own.** When the only usable KiCad is a nightly or a
  release candidate of a new series, edits answer `FORMAT_UPGRADE_BLOCKED` until the project is opened with
  `project_open(path, allow_format_upgrade=true)`. Verified with the real 10.0.6 binary presented as a 10.99
  build; unverified on a real nightly.
* **Copper's own writers emit the KiCad 10.0 format** on every KiCad 10 or later, and KiCad canonicalises it on
  save. They never write a newer stamp over 10.0 content, and never a KiCad 9 file.
* The guards compare format stamps. They cannot tell a 10.0 file from one written by an early nightly before its
  first format change, they do not look inside libraries a project references, and a sub-sheet whose file name
  contains a text variable is not followed.

## kicad-cli commands and flags

Copper does not keep a table of which flag exists in which version. `compat.Caps` asks the binary: a subcommand
exists if its parent's usage line lists it, a flag if the command's `--help` shows it. Answers are cached in
`$COPPER_HOME/cache/cli-caps-<key>.json`, keyed by the binary's path, size and modification time, so an in-place
KiCad update is probed again by itself. When help output cannot be parsed the answer is "present": a failed probe
never drops a flag. Verified on 10.0.6 (7 help calls, 1.3 s, once per binary; 0 calls afterwards).

Flags that are dropped when the active kicad-cli does not list them (`compat.OPTIONAL_FLAGS`); the tool result
then carries a `kicad_notes` line saying what was skipped:

| Command | Flag | Without it |
|---|---|---|
| `pcb drc` | `--refill-zones`, `--save-board` | zones are not refilled before DRC; the refilled board is not saved |
| `pcb export gerbers`, `pdf`, `odb`, `svg` | `--check-zones` | zone fills are not re-checked before the export |
| `pcb export svg` | `--fit-page-to-board` | the plot is not cropped to the board |

All of them exist in 10.0.6 (verified by `tests/data/cli-10.0.json`, the recorded command tree, and a test that
checks every flag literal in Copper's source against it). That KiCad 9.0 lacks them comes from the 9.0 CLI
documentation: **modelled, unverified on a real 9.0 binary**.

Commands that exist only in KiCad 11 development builds (`api-server`, `sch export png`, `pcb export png`,
`sch diff`, `pcb diff`, `pcb export specctra`) are detected by the same probe and shown in the doctor table. Copper
does not use them yet: there is no binary to test against. **Unverified.**

## Known issues by version

`compat.KNOWN_ISSUES`, shown by `copper doctor` and as `kicad.advisories` in `copper_status`. Hand-maintained from
KiCad's release notes; revisit at every KiCad release (see `docs/RELEASING.md`).

| Affects | Issue | What Copper does | Evidence |
|---|---|---|---|
| 10.0.0 to 10.0.6 | zone fills can short through-hole pins and other-net pads (KiCad #25141, #25670), fixed in 10.0.7 | doctor warns; the first DRC on a board with zones adds one `kicad_advisory` line per project | the bug and its fix are from the 10.0.7 release notes: unverified |
| up to 10.0.6 | ERC JSON positions are 100 times too small (KiCad #25582), fixed in 10.0.7 | the scale is measured from the report itself (a symbol's real position against the reported one), so it is right on both sides of the fix; the reported `kicad_version` decides only when no marker is on a symbol | the bug: verified on 10.0.6 (44 markers within 0.01 mm). The fixed behaviour in 10.0.7 and later: **unverified** |
| 10.0.0 to 10.0.7 | spurious error from `sch export bom --preset` (#25834); crash opening a board with a via padstack (#25683) | listed for information | unverified |
| KiCad 11, 10.99 nightlies | the pcbnew Python module is removed | PCB edits answer `PCB_ENGINE_UNAVAILABLE` and say to install KiCad 10.0.x alongside | unverified |

## Update check

`copper doctor` (unless `--offline`) and `copper status` ask GitLab's public tag list for the newest KiCad release:
one GET to a fixed URL with no user data, cached for 24 hours (a failure for one hour) in
`$COPPER_HOME/cache/kicad-latest.json`. `COPPER_NO_UPDATE_CHECK=1` turns it off. The MCP server never makes this
request; `copper_status` only reads the cached answer.

Only releases Copper can use are recommended: the newest `major.minor.patch` tag with major 10 or lower. When
KiCad 11.0.0 is tagged, doctor shows one information row saying that Copper's PCB tools still need KiCad 10.0.x.
Verified against the recorded tag list (which contains `10.0.8-rc1` and the four-part `9.0.9.1`) with the HTTP
layer mocked.

## Other things that follow the KiCad version

* **Config folder.** KiCad appends `<major.minor>` to `KICAD_CONFIG_HOME` itself. Copper 1.0.0 and older exported
  the versioned folder to its board worker, which made KiCad create and use an empty `…/kicad/10.0/10.0`.
  The worker now leaves the variable alone (verified: no nested folder, existing config files untouched).
  `copper doctor` reports a leftover folder; `copper doctor --fix` removes it only when it holds no file.
* **Path variables.** `${KICAD8…11_SYMBOL_DIR}` and friends all resolve for the active install, because a nightly's
  library tables may use either the current or the next major's names. `KICAD_STOCK_DATA_HOME` and
  `KICAD_DOCUMENTS_HOME` are honoured. Whether a nightly names its config folder `10.99` or `11.0` is
  **unverified**; both are accepted for reading, and Copper should not write a nightly's library tables until that
  is checked on a real build.
* **Unpacked symbol libraries.** A library that is a folder of one-symbol `.kicad_sym` files (the layout of
  KiCad's library git repositories) is indexed and read like a packed one, including symbols derived from a
  sibling file. Verified with `Timer` unpacked by kicad-cli 10.0.6 (67 files, rescan 10 ms). The cost of a fully
  unpacked stock library set (about 22,000 files) is not measured.
* **Live API socket.** `api.sock` is preferred; `api-<pid>.sock`, which KiCad uses when a second instance runs, is
  found too. Verified with files in a temporary folder, not with two running KiCads.
* **Board worker dialogs.** pcbnew reports a missing font through a modal dialog that blocked a headless worker
  until the server's timeout. Both workers now switch wx logging off before a board is loaded (verified: a board
  with an unknown font face loads in 0.4 s; the previous worker was still blocked after 45 s). The server's
  subprocess timeout remains the backstop and kills a stuck worker with the board untouched.

## Not done

* A PCB engine for KiCad 11 (IPC API instead of the pcbnew module). It cannot be built or tested before a KiCad 11
  pre-release exists; until then KiCad 10.0.x must stay installed for board edits.
* KiCad 11 drop-ins behind capability checks (direct PNG plots, `diff`, `gerber info`, Specctra export without
  pcbnew).
* Linux nightly and Flatpak layouts (`Install.cli_argv` exists for the Flatpak launcher, discovery does not use it
  yet).
* The CI matrix in `.github/workflows/kicad-matrix.yml` has never run. It is the first real check of everything
  marked modelled or unverified above (KiCad 10.0.8 and 9.0.9 Docker images).
* KiCad 10 design variants (`--variant` on netlist, BOM, position, Gerber, PDF, STEP exports) and the schematic
  PDF are not offered by `fab_export` yet. `compat.variant_args()` is the version-gated helper for it (an
  unsupported command is an error, never a silent fall-back to the default variant).
* Renamed stock symbols (three between the 10.0.6 and 10.0.8 libraries) are not migrated in existing schematics.

## When KiCad releases a new version

1. Install it and run `python tests/test_compat.py`.
2. Regenerate `tests/data/cli-10.0.json` (walk `compat.Caps(...).count()`, dump `d["help"]`) and diff it.
3. Read the release notes; update `compat.KNOWN_ISSUES`, and `compat.FORMATS` if the series is new.
4. Move the rows of this page from modelled or unverified to verified where the new binary proved them.
