# Copper plugin (Claude Code · Codex)

This folder is the plugin that the repository's marketplaces publish: `/.claude-plugin/marketplace.json` for Claude Code and
`/.agents/plugins/marketplace.json` for Codex (same entry; keep the two in step, `tests/test_distribution.py` checks it). It contains:

- `.mcp.json` — starts the Copper MCP server with `npx -y copper-kicad@<version> serve` (the npm launcher; needs
  Node.js 18+ and KiCad, whose bundled Python runs Copper)
- `skills/` — `kicad-design`, `parts-sourcing`, `design-review`, `fab-release` (synced from
  `src/copper_kicad/skills` by `scripts/build_release.py`)
- `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` — the manifests

```bash
# Claude Code
claude plugin marketplace add claynicholson/copper-kicad-mcp
claude plugin install copper@copper

# Codex: add the marketplace, then install "Copper for KiCad" from the
# Plugins directory in the Codex app (the CLI has no plugin install command)
codex plugin marketplace add claynicholson/copper-kicad-mcp
```

**Already installed Copper with the Windows installer (or `copper install`)?** Then Copper is registered twice
and every tool is listed twice. Keep one: `copper uninstall --client claude-code` (the plugin's server and skills
stay), or skip the plugin. `npx -y copper-kicad doctor` warns when both are present.

`AGENTS.snippet.md` is a short block to paste into a project's `AGENTS.md` (Codex) or `CLAUDE.md`.
