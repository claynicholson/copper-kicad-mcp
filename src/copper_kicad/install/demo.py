"""`copper setup --demo [DIR]`: try (or record) setup without touching this computer's real settings.

Before anything else runs, every location Copper and KiCad read or write is pointed inside ``DIR``:

    USERPROFILE / HOME            DIR                         (Path.home(), ~/.cursor, ~/.codex, ~/.claude.json …)
    APPDATA / LOCALAPPDATA        DIR/AppData/Roaming|Local   (Claude Desktop, VS Code, Zed …)
    XDG_CONFIG_HOME / _DATA / _CACHE   DIR/.config|.local/share|.cache
    TEMP / TMP / TMPDIR           DIR/AppData/Local/Temp      (kicad-cli probes use throw-away config roots there)
    COPPER_CONFIG_HOME_OVERRIDE   DIR                         (Copper's own sandbox switch; see clients.py)
    COPPER_HOME                   DIR/.copper
    KICAD_CONFIG_HOME             DIR/AppData/Roaming/kicad   (KiCad appends <major.minor> itself)
    KICAD_DOCUMENTS_HOME          DIR/Documents/KiCad         (env.documents_dir() would otherwise ask Windows)
    KICAD_CACHE_HOME              DIR/AppData/Local/kicad

and, in demo mode:

* secrets (a GitHub link) live in memory only, never in the OS credential store;
* real command-line tools (``claude``, ``gh``, ``code`` …) are never run (``clients._which`` returns None);
* downloads (the Freerouting autorouter) are refused, so nothing large lands on disk;
* every path shown on screen, in the terminal and on the setup page, displays ``DIR`` (and the real home
  folder, which read-only lookups such as ``uv`` on PATH may still name) as a generic home
  (``C:\\Users\\you`` on Windows, ``/home/you`` on Linux, ``/Users/you`` on macOS) so a screenshot or a screen
  recording never shows a real user name. The files themselves are real and live in ``DIR``.

An empty or missing ``DIR`` (or no DIR at all: a new temporary folder) is seeded with a believable starting
point: four AI apps that are installed but not connected, and the settings of an earlier KiCad (9.0) with
the user's own libraries, a colour theme, hotkeys, a plugin and a recent project.

    activate(dir=None, seed=True) -> Path      call before anything reads a path; idempotent
    active() -> Path | None
    seed(dir) -> Path
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ENV = "COPPER_DEMO_HOME"
IS_WIN = sys.platform == "win32"
DOWNLOADS_OFF = ("Demo mode: downloads are switched off, so nothing is fetched. In a normal `copper setup` this "
                 "step downloads Freerouting and a Java runtime (about 120 MB) once.")


def shown_home() -> str:
    """How the demo home is displayed."""
    v = os.environ.get("COPPER_DEMO_SHOW_AS")
    if v:
        return v
    if IS_WIN:
        return r"C:\Users\you"
    return "/Users/you" if sys.platform == "darwin" else "/home/you"


def active() -> Path | None:
    v = os.environ.get(ENV)
    return Path(v) if v else None


def _layout(d: Path) -> dict[str, Path]:
    return {"USERPROFILE": d, "HOME": d,
            "APPDATA": d / "AppData" / "Roaming", "LOCALAPPDATA": d / "AppData" / "Local",
            "XDG_CONFIG_HOME": d / ".config", "XDG_DATA_HOME": d / ".local" / "share",
            "XDG_CACHE_HOME": d / ".cache", "XDG_STATE_HOME": d / ".local" / "state",
            "TEMP": d / "AppData" / "Local" / "Temp", "TMP": d / "AppData" / "Local" / "Temp",
            "TMPDIR": d / "AppData" / "Local" / "Temp",
            "COPPER_CONFIG_HOME_OVERRIDE": d, "COPPER_HOME": d / ".copper",
            "KICAD_CONFIG_HOME": d / "AppData" / "Roaming" / "kicad",
            "KICAD_DOCUMENTS_HOME": d / "Documents" / "KiCad",
            "KICAD_CACHE_HOME": d / "AppData" / "Local" / "kicad"}


def activate(dir: str | os.PathLike | None = None, seed_empty: bool = True) -> Path:  # noqa: A002
    """Point every location at ``dir`` (a new temp folder when None). Must run before any other
    Copper module computes a path; returns the demo home."""
    cur = active()
    if cur is not None and (dir is None or Path(dir).resolve() == cur):
        return cur
    d = Path(dir) if dir else Path(tempfile.mkdtemp(prefix="copper-demo-"))
    d = d.expanduser().resolve()
    if seed_empty and (not d.exists() or not any(d.iterdir())):
        seed(d)
    real_home = str(Path.home())                 # before USERPROFILE/HOME point at the demo
    for k, p in _layout(d).items():
        os.environ[k] = str(p)
    (d / "AppData" / "Local" / "Temp").mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = None                      # re-read TEMP on next use
    os.environ[ENV] = str(d)
    os.environ["COPPER_SECRET_BACKEND"] = "memory"
    os.environ["COPPER_DEMO_SHOW_AS"] = shown_home()
    for k in ("COPPER_GITHUB_CLIENT_ID", "COPPER_GH", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPPER_KICAD_VERSION",
              "COPPER_KICAD_VERSION_FROM_PREFS"):
        os.environ.pop(k, None)
    from ..link import redact
    redact.alias(str(d), shown_home())
    # read-only lookups outside the demo (a tool on PATH, a source checkout) still name the real home:
    # show that as the generic home too, so a recording never carries the real user name
    redact.alias(real_home, shown_home())
    return d


class DownloadsOff(RuntimeError):
    message = DOWNLOADS_OFF
    fix = None


def refuse_download() -> None:
    if active() is not None:
        raise DownloadsOff(DOWNLOADS_OFF)


# ── seeding ───────────────────────────────────────────────────────────────
_SYM_STOCK = ("Device", "power", "Connector", "MCU_RaspberryPi", "Regulator_Linear")
_FP_STOCK = ("Resistor_SMD", "Capacitor_SMD", "Connector_USB", "Package_QFP", "LED_SMD")


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def seed(d: str | os.PathLike) -> Path:
    """A believable machine: AI apps installed but not connected, settings of an earlier KiCad."""
    d = Path(d).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    roam, docs = d / "AppData" / "Roaming", d / "Documents"
    # AI apps (installed, Copper not connected yet)
    _w(roam / "Claude" / "claude_desktop_config.json", json.dumps({"preferences": {"quickEntryShortcut": "off"}}, indent=2) + "\n")
    _w(d / ".claude.json", json.dumps({"numStartups": 12, "theme": "dark"}, indent=2) + "\n")
    (d / ".claude").mkdir(exist_ok=True)
    _w(d / ".codex" / "config.toml", '# Codex settings\napproval_policy = "on-request"\n')
    _w(d / ".cursor" / "mcp.json", json.dumps({"mcpServers": {}}, indent=2) + "\n")
    # the user's own KiCad work: a library, a footprint library, a project
    libs = docs / "KiCad" / "libs"
    _w(libs / "MyParts.kicad_sym", '(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor"))\n')
    _w(libs / "MyFootprints.pretty" / "TestPad_1mm.kicad_mod", '(footprint "TestPad_1mm" (version 20240108) (layer "F.Cu"))\n')
    proj = docs / "KiCad" / "projects" / "led-badge" / "led-badge.kicad_pro"
    _w(proj, json.dumps({"board": {"design_settings": {"rules": {"min_clearance": 0.15, "min_track_width": 0.15}}},
                         "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.25},
                                                      {"name": "Power", "clearance": 0.2, "track_width": 0.5}]},
                         "meta": {"filename": "led-badge.kicad_pro", "version": 1}}, indent=2) + "\n")
    # KiCad 9.0 settings (the earlier version this person used)
    k9 = roam / "kicad" / "9.0"
    sym = "".join(f'\t(lib (name "{n}") (type "KiCad") (uri "${{KICAD9_SYMBOL_DIR}}/{n}.kicad_sym") (options "") (descr ""))\n'
                  for n in _SYM_STOCK)
    fp = "".join(f'\t(lib (name "{n}") (type "KiCad") (uri "${{KICAD9_FOOTPRINT_DIR}}/{n}.pretty") (options "") (descr ""))\n'
                 for n in _FP_STOCK)
    _w(k9 / "sym-lib-table", "(sym_lib_table\n\t(version 7)\n" + sym
       + '\t(lib (name "MyParts") (type "KiCad") (uri "${MY_LIBS}/MyParts.kicad_sym") (options "") (descr "my own parts"))\n)\n')
    _w(k9 / "fp-lib-table", "(fp_lib_table\n\t(version 7)\n" + fp
       + '\t(lib (name "MyFootprints") (type "KiCad") (uri "${MY_LIBS}/MyFootprints.pretty") (options "") (descr ""))\n)\n')
    _w(k9 / "colors" / "Midnight.json", json.dumps({"meta": {"name": "Midnight", "version": 5}}, indent=2) + "\n")
    _w(k9 / "user.hotkeys", "common.Control.zoomIn\tCtrl+=\ncommon.Control.zoomOut\tCtrl+-\n")
    _w(k9 / "kicad.json", json.dumps({"system": {"file_history": [str(proj)]}}, indent=2) + "\n")
    _w(k9 / "kicad_common.json", json.dumps({"environment": {"vars": {"MY_LIBS": str(libs).replace("\\", "/")}},
                                             "api": {"enable_server": False}}, indent=2) + "\n")
    _w(k9 / "eeschema.json", json.dumps({"appearance": {"show_hidden_pins": False}}, indent=2) + "\n")
    _w(k9 / "pcbnew.json", json.dumps({"appearance": {"show_track_clearance": 1}}, indent=2) + "\n")
    _w(docs / "KiCad" / "9.0" / "scripting" / "plugins" / "board_stats" / "__init__.py",
       "# a small action plugin of the user's\n")
    return d
