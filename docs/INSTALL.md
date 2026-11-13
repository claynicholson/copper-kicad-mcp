# Installing Copper

Copper needs **[KiCad 10](https://www.kicad.org/download/)** (10.0.x). KiCad 9 can view and check designs but
not edit them ([COMPATIBILITY.md](COMPATIBILITY.md)). Everything installs per user, without admin rights.

- [Windows installer](#windows-installer) · [Claude Desktop](#claude-desktop-mcpb) · [Claude Code](#claude-code) ·
  [npx (any MCP app)](#npx-any-mcp-app) · [One-line installers](#one-line-installers) ·
  [Setup and linking](#setup-and-linking) · [Troubleshooting](#troubleshooting) · [Uninstall](#uninstall)

## Windows installer

Download `Copper-for-KiCad-1.1.0-Setup.exe` from the
[latest release](https://github.com/claynicholson/copper-kicad-mcp/releases/latest) and click through it, as you
would KiCad's installer. Tick your AI apps on the *Components* page (apps not found yet are listed unticked).
Freerouting, for autorouting, is ticked by default (about 120 MB download, once). The installer brings its own Python.

No installer wanted? `Copper-for-KiCad-1.1.0-portable-win64.zip` from the same release: unzip it somewhere
permanent and run `install.cmd`.

**Windows SmartScreen.** Until the installer is code-signed, Windows may say *Windows protected your PC*. Check the
file against `SHA256SUMS.txt` from the same release (`Get-FileHash .\Copper-for-KiCad-1.1.0-Setup.exe`), then
choose *More info → Run anyway*.

## Claude Desktop (.mcpb)

Download `copper-1.1.0.mcpb` from the [latest release](https://github.com/claynicholson/copper-kicad-mcp/releases/latest)
and double-click it (or Settings → Extensions → Install extension). The Windows installer registers Claude
Desktop too, so you need only one of the two.

## Claude Code

```bash
claude plugin marketplace add claynicholson/copper-kicad-mcp
claude plugin install copper@copper
```

The plugin brings the Copper server (through `npx`, so Node.js 18+ is needed) and four design skills:
`kicad-design`, `parts-sourcing`, `design-review` and `fab-release`. Without the plugin:
`claude mcp add copper -s user -- npx -y copper-kicad`.

Pick one: if the Windows installer already registered Copper with Claude Code, every tool would be listed twice
(`copper doctor` warns). Keep the plugin and run `copper uninstall --client claude-code`, or skip the plugin.

## npx (any MCP app)

The npm package [`copper-kicad`](https://www.npmjs.com/package/copper-kicad) runs Copper on the Python that comes
with KiCad. It needs Node.js 18+ and KiCad, nothing else. The easiest way is to let Copper write the config:

```bash
npx -y copper-kicad setup
```

Or paste the entry yourself:

| App | File | Entry |
|---|---|---|
| Cursor | `~/.cursor/mcp.json` | `{"mcpServers": {"copper": {"command": "npx", "args": ["-y", "copper-kicad"]}}}` |
| Claude Desktop | Settings → Developer → Edit config | same as Cursor |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | same as Cursor |
| VS Code | `.vscode/mcp.json` or *MCP: Open User Configuration* | `{"servers": {"copper": {"type": "stdio", "command": "npx", "args": ["-y", "copper-kicad"]}}}` |
| Codex | `~/.codex/config.toml` | `[mcp_servers.copper]` with `command = "npx"`, `args = ["-y", "copper-kicad"]`, `tool_timeout_sec = 600` |
| Anything else | its MCP settings | command `npx`, args `-y copper-kicad`, transport stdio |

To stay on one version instead of the latest, use `"args": ["-y", "copper-kicad@1.1.0"]` (the Claude Code plugin
does this).

One click: [Add to Cursor](https://cursor.com/install-mcp?name=copper&config=eyJjb21tYW5kIjoibnB4IiwiYXJncyI6WyIteSIsImNvcHBlci1raWNhZCJdfQ%3D%3D) ·
[Install in VS Code](https://vscode.dev/redirect/mcp/install?name=copper&config=%7B%22command%22%3A%22npx%22%2C%22args%22%3A%5B%22-y%22%2C%22copper-kicad%22%5D%7D).

Other `copper` commands work the same way: `npx -y copper-kicad doctor`, `npx -y copper-kicad link --verify`.
`COPPER_PYTHON` points the launcher at another Python 3.10+; `COPPER_KICAD_CLI` at an unusual KiCad. If the
Python has no Pillow, the launcher installs it once from PyPI into `~/.copper/runtime`, never into KiCad's own Python.

## One-line installers

Install Copper as a `copper` command from the release wheel (checked against the release's `SHA256SUMS.txt`) and
run `copper setup`:

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/claynicholson/copper-kicad-mcp/main/installer/install.ps1 | iex"
```

```bash
# macOS / Linux
curl -LsSf https://raw.githubusercontent.com/claynicholson/copper-kicad-mcp/main/installer/install.sh | sh
```

Both install [uv](https://docs.astral.sh/uv/) from astral.sh if it is missing, then put Copper in its own Python 3.12
environment: Copper's wheel and `SHA256SUMS.txt` come from this repository's GitHub release, while uv downloads
Python 3.12 and Pillow (Copper's one dependency). uv adds its tool folder to your PATH (the user PATH on Windows,
your shell profile on macOS and Linux) unless `COPPER_NO_MODIFY_PATH=1` is set. On Windows, an upgrade first stops
Copper's own running server processes, which lock its files; restart your AI apps afterwards.
You can change what they do with these environment variables:

| Variable | Effect |
|---|---|
| `COPPER_VERSION=1.1.0` | Installs that release instead of the latest. |
| `COPPER_SETUP_ARGS="--tty"` | Passes these arguments to `copper setup`. |
| `COPPER_NO_SETUP=1` | Installs only; `copper setup` is not run. |
| `COPPER_SOURCE=<wheel path or URL>` | Installs this wheel. There is no download and no checksum check. |
| `COPPER_NO_MODIFY_PATH=1` | uv does not add its tool folder to your PATH or shell profile. |
| `COPPER_NO_DESKTOP=1` | Linux only: no "Copper setup" entry is added to the applications menu. |

More for macOS and Linux (uv or pipx straight from the wheel URL): [installer/linux/README.md](../installer/linux/README.md).
The macOS and Linux routes have not yet been run end to end; please open an issue if one fails.

## Setup and linking

`copper setup` opens a setup page in your browser (the Windows installer opens it for you). The page runs on your
own computer, loads nothing from the internet, and has six steps:

1. **KiCad**: the version it found, or what to do if KiCad is missing or is only KiCad 9.
2. **Your KiCad setup**: what to bring along from it (see below).
3. **AI apps**: the apps it found, connected in one click.
4. **GitHub**: optional.
5. **Autorouter**: the optional Freerouting download.
6. **Try it**: a test call that starts Copper the way your AI app will, then a first prompt to copy.

Without a desktop, or with `copper setup --tty`, the same questions are asked in the terminal. It asks before each
change; `copper setup --yes` asks nothing and changes nothing in KiCad's own settings. The page works with the
keyboard alone (Alt+← / Alt+→ move between steps) and with screen readers.

**Try it first, on a throw-away folder.** `npx -y copper-kicad setup --demo` (or `copper setup --demo`) runs the
whole flow against a new temporary folder that holds sample AI apps and KiCad 9 settings, so you can click through
every step. No AI app, KiCad setting or token on your computer is touched. Paths on screen read as a generic home
such as `C:\Users\you`. `--demo DIR` uses a folder of your choice. The same flag works for `copper link`,
`copper install`, `copper uninstall`, `copper doctor`, `copper status` and `copper config`. In demo mode, the
Freerouting download is switched off, tools such as `claude`, `gh` and `code` are never run, and a GitHub link is
held in memory only. Through `npx`, the launcher's own files go to a temporary folder as well.

**Already use KiCad?** Setup shows what you have and asks about each item: libraries (including your own), path
variables, colour themes, hotkeys, plugins, recent projects, design rules, and KiCad's scripting API. With
settings from an older KiCad it offers to copy them across; the old files are only read. Every file that changes
is backed up first, and `copper setup --undo` puts everything back.

```bash
copper link                  # every AI app on this computer, connected or not; one answer connects them all
copper link --verify         # start Copper the way each app would and check that it answers
copper link cursor           # one app (claude-desktop, claude-code, codex, cursor, vscode, windsurf, zed, gemini, copilot-cli)
copper link github           # optional
copper link --unlink cursor
```

Each settings file Copper touches is backed up once as `*.copper-backup`, and Copper's entry is merged in, so
your other servers, comments and formatting stay. Copper never asks for a password and does not link AI-vendor
accounts: each app keeps its own login.

## Troubleshooting

Installed through `npx`? Type `npx -y copper-kicad` wherever these say `copper`
(`npx -y copper-kicad doctor`).

```bash
copper doctor          # green/red checks with the exact fix for each problem
copper status          # version, KiCad, and which apps Copper is registered with
copper install --client claude-desktop --dry-run   # show the config change without writing it
copper config --client generic                     # plain stdio JSON for any other MCP client
```

| Symptom | Fix |
|---|---|
| Claude Desktop doesn't show Copper | Quit Claude Desktop from the tray icon and reopen it. |
| *KiCad was not found* | Install KiCad 10, or set `COPPER_KICAD_CLI` to the path of `kicad-cli`. |
| `npx` says *no Python 3.10+ found* | Install KiCad 10 (its Python is used), or set `COPPER_PYTHON`. |
| Codex times out on routing or exports | `copper install --client codex` sets `tool_timeout_sec = 600` and `startup_timeout_sec = 60`. |
| Schematic edits are refused: *SCHEMATIC_OPEN* | Save and close the Schematic Editor (KiCad has no live schematic API), or use `while_open="revert"` and File → Revert. |
| An AI app shows no Copper tools | `copper link --verify` names the step that fails; `copper link <app>` rewrites the entry. Restart the app. |
| Autorouting is unavailable | `copper freerouting --install` downloads Freerouting and a Java runtime (about 120 MB, once). |
| Just upgraded KiCad | Run `copper setup`: it finds the older version's settings and asks what to copy. |

## Uninstall

```bash
copper uninstall       # remove Copper from every app; lists the *.copper-backup files it kept
# set up through npx? then the same command is:
npx -y copper-kicad uninstall
```

Then remove the program: Windows *Apps & features → Copper for KiCad*, or `uv tool uninstall copper-kicad` after a
one-line install. The npx route leaves only `~/.copper`: delete it after `uninstall`, since the app entries that
npx setup wrote point into it.
