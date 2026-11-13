# copper-kicad

An MCP server that connects your AI app to KiCad, so it can design boards you can order: schematic,
LCSC/JLCPCB parts, PCB placement and routing, KiCad's own ERC and DRC, renders and fab files.

This npm package is a small launcher. It runs Copper (pure Python) on the Python that comes with KiCad, so you
need **KiCad 10** and **Node.js 18+**, nothing else.

```json
{
  "mcpServers": {
    "copper": { "command": "npx", "args": ["-y", "copper-kicad"] }
  }
}
```

Paste that into Cursor, Windsurf or Claude Desktop (VS Code, Codex and Zed use their own formats, see the
[install guide](https://github.com/claynicholson/copper-kicad-mcp/blob/main/docs/INSTALL.md#npx-any-mcp-app)), or let
Copper write it for every app it finds and walk you through KiCad, libraries and Freerouting:

```bash
npx -y copper-kicad setup
```

Every other argument goes to the `copper` command line: `npx -y copper-kicad doctor`, `... link --verify`, `... --help`.

| Variable | Use |
|---|---|
| `COPPER_PYTHON` | Run Copper on this Python (3.10+) instead of KiCad's |
| `COPPER_KICAD_CLI` | Path to `kicad-cli` if KiCad is somewhere unusual |
| `COPPER_HOME` | Copper's data folder (default `~/.copper`) |

If the chosen Python has no Pillow, the launcher installs it once from PyPI into `~/.copper/runtime` (never into
KiCad's or the system Python). `setup`, `install`, `link` and `config` run from a copy in `~/.copper/runtime/npm-<version>`,
so the AI apps they connect keep working after npm clears its cache. To remove Copper from those apps, run
`npx -y copper-kicad uninstall` before deleting `~/.copper`. `setup --demo` keeps everything in a temporary folder.

Docs, Windows installer and source: <https://github.com/claynicholson/copper-kicad-mcp> · <https://coppereda.com>

MIT licensed.
