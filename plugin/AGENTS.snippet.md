<!-- Copper for KiCad — paste into your project's AGENTS.md (Codex) or CLAUDE.md -->
## KiCad work: use Copper

This repository contains KiCad projects. Use the **copper** MCP server (`copper_status` first). Do not hand-edit
files blindly.

- **Loop:** plan → `parts_search` (asks the project's parts providers; LCSC with JLC Basic first by default) → `sch_apply` in big batches (connect pins to nets *by
  name*, never by coordinates) → `verify` → `render` and look → fix findings using their `fix` hints → repeat.
- **PCB:** `pcb_sync` → `pcb_setup` (the fab profile's rules) → `pcb_place` → `pcb_route` → `pcb_pour` → `verify(what=["erc","drc"])`.
- **Release:** `fab_export` only after 0 ERC/DRC errors and a 100% sourced BOM. It writes the pack for the project's
  fab profile (`copper_status` shows it; JLCPCB by default).
- Every part carries the project's part-number field (`LCSC` by default) plus `MPN`. Never invent part numbers.
- Direct `.kicad_sch`/`.kicad_pcb` edits are allowed for anything the tools don't cover. Close the file in KiCad
  first, then call `verify`.
- If a Copper tool errors, follow its `fix` hint. If Copper itself looks broken, ask the user to run `copper doctor`.
- Long operations (routing, exports) can take minutes: give the client a tool timeout of 600 s (Codex: `tool_timeout_sec = 600` for `[mcp_servers.copper]`).
