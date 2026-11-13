"""Register Copper with every AI client on this machine.

Each client gets an entry that launches Copper by ABSOLUTE path (never relying
on PATH, which GUI apps such as Claude Desktop often don't inherit), merged
into the client's own config file without touching anything else:

* JSON / JSONC files are edited span-wise (see :mod:`jsonc`) — comments,
  ordering and other servers are preserved byte for byte;
* Codex TOML is edited as text, replacing only ``[mcp_servers.copper*]`` tables;
* the first time Copper touches a file it saves ``<file>.copper-backup``.

Testing / sandboxing: set ``COPPER_CONFIG_HOME_OVERRIDE=<dir>`` and every path
below resolves inside that directory (``<dir>/AppData/Roaming`` for
%APPDATA% etc.); the ``claude`` CLI is never invoked in that mode.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import SERVER_NAME, __version__
from . import jsonc

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
BACKUP_SUFFIX = ".copper-backup"
_NO_WINDOW = 0x08000000 if IS_WIN else 0


# ── locations ─────────────────────────────────────────────────────────────
def override_home() -> Path | None:
    v = os.environ.get("COPPER_CONFIG_HOME_OVERRIDE")
    return Path(v) if v else None


def home() -> Path:
    return override_home() or Path.home()


def appdata() -> Path:
    o = override_home()
    if o:
        return o / "AppData" / "Roaming"
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def localappdata() -> Path:
    o = override_home()
    if o:
        return o / "AppData" / "Local"
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def xdg_config() -> Path:
    o = override_home()
    if o:
        return o / ".config"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def mac_support() -> Path:
    return home() / "Library" / "Application Support"


def copper_home() -> Path:
    """Copper's own data dir (state, installed-clients manifest, tools)."""
    o = override_home()
    if o:
        return o / ".copper"
    return Path(os.environ.get("COPPER_HOME") or Path.home() / ".copper")


def _which(name: str) -> str | None:
    if override_home():
        return None          # sandboxed: never detect or call real CLIs
    return shutil.which(name)


# ── launch spec ───────────────────────────────────────────────────────────
@dataclass
class ServerSpec:
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)

    def entry(self) -> dict:
        d = {"command": self.command, "args": list(self.args)}
        if self.env:
            d["env"] = dict(self.env)
        return d

    def argv(self) -> list[str]:
        return [self.command, *self.args]


def _package_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _ephemeral(path: str) -> bool:
    """True for uv's throw-away environments (``uvx``/``uv tool run``), which
    live in the cache (``…/uv/cache/archive-v0/<hash>/…``)."""
    p = path.lower().replace("\\", "/")
    return "/archive-v0/" in p or "/uv/cache/" in p or "/.cache/uv/" in p


def _installed_in_site_packages() -> bool:
    p = str(_package_dir()).lower().replace("\\", "/")
    return "site-packages" in p or "dist-packages" in p


def server_spec(launcher: str | None = None) -> ServerSpec:
    """How a client should start Copper, using absolute paths.

    Order: explicit ``launcher`` / ``COPPER_LAUNCHER`` → the ``copper`` entry
    point next to the running interpreter (pip/uv-tool installs) → ``python -m
    copper_kicad`` with PYTHONPATH when running from a source checkout.
    """
    launcher = launcher or os.environ.get("COPPER_LAUNCHER")
    if launcher:
        lp = Path(launcher)
        if lp.suffix.lower() == ".py" or lp.name.lower().startswith("python"):
            return ServerSpec(str(lp.resolve()), ["-m", "copper_kicad", "serve"])
        return ServerSpec(str(lp.resolve()) if lp.exists() else launcher, ["serve"])
    exe = Path(sys.executable)
    if _ephemeral(str(_package_dir())):
        # `uvx copper-kicad setup` runs from uv's cache; registering that path would
        # break on the next `uv cache clean`. Register uvx itself (absolute path).
        uvx = shutil.which("uvx") or shutil.which("uv")
        if uvx:
            if Path(uvx).stem.lower() == "uvx":
                return ServerSpec(str(Path(uvx).resolve()), ["--from", "copper-kicad", "copper", "serve"])
            return ServerSpec(str(Path(uvx).resolve()), ["tool", "run", "--from", "copper-kicad", "copper", "serve"])
    if _installed_in_site_packages():
        for cand in (exe.parent / ("copper.exe" if IS_WIN else "copper"),
                     exe.parent / "Scripts" / "copper.exe"):
            if cand.exists():
                return ServerSpec(str(cand), ["serve"])
        return ServerSpec(str(exe), ["-m", "copper_kicad", "serve"])
    # Source checkout: bootstrap sys.path explicitly. (PYTHONPATH is not enough:
    # KiCad's bundled python.exe, a common dev interpreter, ignores it.)
    src = str(_package_dir().parent)
    boot = (f"import sys; sys.path.insert(0, {src!r}); "
            "from copper_kicad.cli import main; sys.exit(main(['serve']))")
    return ServerSpec(str(exe), ["-c", boot])


# ── clients ───────────────────────────────────────────────────────────────
@dataclass
class Target:
    """One config file a client reads."""
    path: Path
    fmt: str                 # json | toml | claude-cli
    key_path: list[str]      # path to the servers object (json)
    note: str = ""


@dataclass
class Client:
    id: str
    name: str
    detect: callable
    targets: callable        # () -> list[Target]
    shape: callable          # (ServerSpec) -> dict entry
    restart_hint: str = ""
    docs: str = ""

    def detected(self) -> bool:
        try:
            return bool(self.detect())
        except Exception:  # noqa: BLE001
            return False


def _plain(spec: ServerSpec) -> dict:
    return spec.entry()


def _typed_stdio(spec: ServerSpec) -> dict:
    return {"type": "stdio", **spec.entry()}


def _gemini(spec: ServerSpec) -> dict:
    return {**spec.entry(), "timeout": 600000}


def _zed(spec: ServerSpec) -> dict:
    d = {"source": "custom", **spec.entry()}
    d.setdefault("env", {})
    return d


def _copilot(spec: ServerSpec) -> dict:
    return {"type": "local", **spec.entry(), "tools": ["*"]}


# Claude Desktop ------------------------------------------------------------
def _claude_desktop_dirs() -> list[Path]:
    out = []
    if IS_WIN:
        out.append(appdata() / "Claude")
        pk = localappdata() / "Packages"
        if pk.is_dir():
            for d in sorted(pk.glob("Claude_*")):
                out.append(d / "LocalCache" / "Roaming" / "Claude")
    elif IS_MAC:
        out.append(mac_support() / "Claude")
    else:
        out.append(xdg_config() / "Claude")
    return out


def _claude_desktop_targets() -> list[Target]:
    ts = []
    dirs = _claude_desktop_dirs()
    for i, d in enumerate(dirs):
        # the primary (%APPDATA%) file is always written; MSIX copies only when present
        if i == 0 or d.exists():
            ts.append(Target(d / "claude_desktop_config.json", "json", ["mcpServers"],
                             "MSIX (Microsoft Store) build" if "Packages" in str(d) else ""))
    return ts


def _claude_desktop_detect() -> bool:
    if any(d.exists() for d in _claude_desktop_dirs()):
        return True
    if IS_WIN and not override_home():
        return (localappdata() / "AnthropicClaude").exists() or (localappdata() / "Programs" / "Claude").exists()
    if IS_MAC and not override_home():
        return Path("/Applications/Claude.app").exists()
    return False


# Claude Code ---------------------------------------------------------------
def _claude_code_json() -> Path:
    # Claude Code keeps its user config at $CLAUDE_CONFIG_DIR/.claude.json when that is set.
    if not override_home() and os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / ".claude.json"
    return home() / ".claude.json"


def _claude_code_targets() -> list[Target]:
    return [Target(_claude_code_json(), "claude-cli", ["mcpServers"], "user scope")]


def _claude_code_detect() -> bool:
    return bool(_which("claude")) or _claude_code_json().exists() or (home() / ".claude").is_dir()


# Codex ---------------------------------------------------------------------
def _codex_home() -> Path:
    if not override_home() and os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"])
    return home() / ".codex"


# VS Code -------------------------------------------------------------------
def _vscode_user_dirs() -> list[Path]:
    names = ["Code", "Code - Insiders", "VSCodium"]
    if IS_WIN:
        base = appdata()
    elif IS_MAC:
        base = mac_support()
    else:
        base = xdg_config()
    return [base / n / "User" for n in names]


def _vscode_targets() -> list[Target]:
    dirs = _vscode_user_dirs()
    ts = [Target(d / "mcp.json", "json", ["servers"], d.parent.name) for d in dirs if d.parent.exists()]
    return ts or [Target(dirs[0] / "mcp.json", "json", ["servers"], "Code")]


def _vscode_detect() -> bool:
    return any(d.parent.exists() for d in _vscode_user_dirs()) or bool(_which("code"))


# Zed -----------------------------------------------------------------------
def _zed_settings() -> Path:
    if IS_WIN:
        return appdata() / "Zed" / "settings.json"
    if IS_MAC:
        return home() / ".config" / "zed" / "settings.json"
    return xdg_config() / "zed" / "settings.json"


# Windsurf / Devin ----------------------------------------------------------
def _windsurf_targets() -> list[Target]:
    ts = [Target(home() / ".codeium" / "windsurf" / "mcp_config.json", "json", ["mcpServers"], "Windsurf")]
    devin = xdg_config() / "devin"
    if devin.exists():
        ts.append(Target(devin / "mcp_config.json", "json", ["mcpServers"], "Devin Desktop"))
    return ts


CLIENTS: list[Client] = [
    Client("claude-desktop", "Claude Desktop", _claude_desktop_detect, _claude_desktop_targets, _plain,
           "Quit Claude Desktop from the tray icon and reopen it.",
           "https://modelcontextprotocol.io/quickstart/user"),
    Client("claude-code", "Claude Code", _claude_code_detect, _claude_code_targets, _typed_stdio,
           "Start a new `claude` session (or run /mcp).", "https://code.claude.com/docs/en/mcp"),
    Client("codex", "OpenAI Codex", lambda: bool(_which("codex")) or _codex_home().exists(),
           lambda: [Target(_codex_home() / "config.toml", "toml", ["mcp_servers"])], _plain,
           "Restart Codex (CLI, app or IDE extension).", "https://developers.openai.com/codex/mcp"),
    Client("cursor", "Cursor", lambda: (home() / ".cursor").exists() or bool(_which("cursor")),
           lambda: [Target(home() / ".cursor" / "mcp.json", "json", ["mcpServers"])], _plain,
           "Cursor picks it up automatically (Settings → MCP to toggle).", "https://docs.cursor.com/context/mcp"),
    Client("vscode", "VS Code (Copilot)", _vscode_detect, _vscode_targets, _typed_stdio,
           "Run 'MCP: List Servers' in VS Code and start copper.",
           "https://code.visualstudio.com/docs/copilot/chat/mcp-servers"),
    Client("windsurf", "Windsurf / Devin", lambda: (home() / ".codeium" / "windsurf").exists() or (xdg_config() / "devin").exists(),
           _windsurf_targets, _plain, "Press refresh in the Cascade MCP panel.", "https://docs.windsurf.com/windsurf/cascade/mcp"),
    Client("zed", "Zed", lambda: _zed_settings().parent.exists() or bool(_which("zed")),
           lambda: [Target(_zed_settings(), "json", ["context_servers"])], _zed,
           "Zed reloads settings automatically.", "https://zed.dev/docs/ai/mcp"),
    Client("gemini", "Gemini CLI", lambda: (home() / ".gemini").exists() or bool(_which("gemini")),
           lambda: [Target(home() / ".gemini" / "settings.json", "json", ["mcpServers"])], _gemini,
           "Start a new `gemini` session (/mcp lists servers).", "https://github.com/google-gemini/gemini-cli"),
    Client("copilot-cli", "GitHub Copilot CLI", lambda: (home() / ".copilot").exists() or bool(_which("copilot")),
           lambda: [Target(home() / ".copilot" / "mcp-config.json", "json", ["mcpServers"])], _copilot,
           "Start a new `copilot` session.", "https://docs.github.com/copilot"),
]
BY_ID = {c.id: c for c in CLIENTS}
ALIASES = {"claude": "claude-desktop", "desktop": "claude-desktop", "claude_desktop": "claude-desktop",
           "claudecode": "claude-code", "claude_code": "claude-code", "code": "vscode", "vs-code": "vscode",
           "devin": "windsurf", "gemini-cli": "gemini", "copilot": "copilot-cli"}


def resolve_names(names: list[str] | str | None, detected_only_for_all=True) -> list[Client]:
    if names is None or names == "all" or names == ["all"]:
        return [c for c in CLIENTS if c.detected()] if detected_only_for_all else list(CLIENTS)
    if isinstance(names, str):
        names = [n for n in re.split(r"[,\s]+", names) if n]
    out = []
    for n in names:
        k = ALIASES.get(n.lower(), n.lower())
        if k == "all":
            out += [c for c in CLIENTS if c.detected() and c not in out]
            continue
        if k not in BY_ID:
            raise ValueError(f"unknown client '{n}'. Known: {', '.join(BY_ID)}")
        if BY_ID[k] not in out:
            out.append(BY_ID[k])
    return out


def detect() -> list[dict]:
    return [{"id": c.id, "name": c.name, "detected": c.detected(),
             "config": [str(t.path) for t in c.targets()]} for c in CLIENTS]


# ── file helpers ──────────────────────────────────────────────────────────
class ConfigFileError(Exception):
    """A client config Copper must not edit (wrong encoding, unwritable …)."""


def _read(p: Path) -> str:
    """Config text. UTF-8 (BOM kept as U+FEFF); bytes that are not UTF-8 (a
    cp1252 'é' typed in Notepad) survive a read/write round trip unchanged via
    surrogateescape. UTF-16/32 files (PowerShell 5 `Out-File`/`Set-Content
    -Encoding Unicode`) are refused: the client itself cannot read them."""
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return ""
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff") or raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (
            len(raw) >= 2 and (raw[0] == 0 or raw[1] == 0)):
        raise ConfigFileError(
            f"{p} is saved as UTF-16, which the app cannot read either — re-save it as UTF-8 "
            "(VS Code: 'Save with Encoding' → UTF-8; PowerShell: Set-Content -Encoding utf8), then run `copper install` again")
    if raw.startswith(b"\xef\xbb\xbf"):
        return "﻿" + raw[3:].decode("utf-8", "surrogateescape")
    return raw.decode("utf-8", "surrogateescape")


def _write(p: Path, text: str):
    # Follow a symlinked config (dotfile managers) instead of replacing the link with a file.
    try:
        if p.is_symlink():
            p = p.resolve()
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and not Path(str(p) + BACKUP_SUFFIX).exists():
        shutil.copy2(p, str(p) + BACKUP_SUFFIX)
    tmp = p.with_name(p.name + ".copper-tmp")
    data = text.encode("utf-8", "surrogateescape")   # a leading ﻿ (original BOM) is written back as-is
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:  # file briefly locked by the client (Windows)
                time.sleep(0.2 * (attempt + 1))
        try:
            os.replace(tmp, p)
        except PermissionError as e:
            raise ConfigFileError(
                f"cannot write {p} ({e.strerror or e}) — it is read-only or held open by another program; "
                "clear the read-only flag / close the app, then run `copper install` again") from None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _diff(path: Path, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"{path} (before)", f"{path} (after)", n=2))


# ── TOML (Codex) ──────────────────────────────────────────────────────────
_HDR = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _toml_key(raw: str) -> str:
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|([A-Za-z0-9_-]+)', raw)
    return ".".join(a or b or c for a, b, c in parts)


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


_KEYPART = r"""(?:"(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+)"""
_KEYPATH = re.compile(r"\s*(" + _KEYPART + r"(?:\s*\.\s*" + _KEYPART + r")*)\s*=\s*")
_TOML_STR = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_TOML_OWNED = ("command", "args", "startup_timeout_sec", "tool_timeout_sec")
CODEX_EXTRA = {"startup_timeout_sec": 60, "tool_timeout_sec": 600}


def _toml_line(s: str, in_ml):
    """(bracket depth change, open multi-line string delimiter) after scanning one line."""
    delta, i, n = 0, 0, len(s)
    while i < n:
        if in_ml:
            j = s.find(in_ml, i)
            if j < 0:
                return delta, in_ml
            i, in_ml = j + 3, None
            continue
        c = s[i]
        if s.startswith('"""', i) or s.startswith("'''", i):
            in_ml, i = s[i:i + 3], i + 3
            continue
        if c == '"':
            m = _TOML_STR.match(s, i)
            i = m.end() if m else n
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "#":
            break
        if c in "[{":
            delta += 1
        elif c in "]}":
            delta -= 1
        i += 1
    return delta, in_ml


def _toml_scan(text: str, table: str):
    """Classify the lines of a TOML document relative to ``table``.

    Returns ``(lines, own, stray, keys)``: ``own`` = indices of lines in the
    ``[table]`` / ``[table.*]`` sections (headers included); ``stray`` = key
    lines elsewhere that also define ``table`` — ``copper = { … }`` under
    ``[mcp_servers]`` or a dotted ``mcp_servers.copper.command = …`` — which
    would make a second ``[table]`` header a duplicate-key error; ``keys`` maps
    each key line's start index to (section, key, line indices)."""
    lines = text.splitlines(True)
    own: list[int] = []
    stray: list[int] = []
    keys: list[tuple[str, str, list[int]]] = []
    cur, i = "", 0
    inside = lambda sec: sec == table or sec.startswith(table + ".")  # noqa: E731
    while i < len(lines):
        ln = lines[i]
        m = _HDR.match(ln)
        if m:
            cur = _toml_key(m.group(1))
            if inside(cur):
                own.append(i)
            i += 1
            continue
        depth, ml = _toml_line(ln, None)
        j = i
        while (depth > 0 or ml) and j + 1 < len(lines):
            j += 1
            d, ml = _toml_line(lines[j], ml)
            depth += d
        span = list(range(i, j + 1))
        km = _KEYPATH.match(ln)
        if inside(cur):
            own += span
            if km:
                keys.append((cur, _toml_key(km.group(1)), span))
        elif km:
            full = (cur + "." if cur else "") + _toml_key(km.group(1))
            if inside(full):
                stray += span
        i = j + 1
    return lines, own, stray, keys


def _collapse_blank(text: str) -> str:
    return re.sub(r"(\r?\n){3,}", lambda m: m.group(1) * 2, text)


def toml_remove(text: str, table: str) -> str:
    lines, own, stray, _ = _toml_scan(text, table)
    drop = set(own) | set(stray)
    return _collapse_blank("".join(ln for i, ln in enumerate(lines) if i not in drop))


def toml_block(table: str, spec: ServerSpec, extra: dict | None = None, nl="\n") -> str:
    lines = [f"[{table}]",
             f"command = {_toml_str(spec.command)}",
             "args = [" + ", ".join(_toml_str(a) for a in spec.args) + "]"]
    for k, v in (extra or {}).items():
        lines.append(f"{k} = {json.dumps(v)}")
    if spec.env:
        lines += ["", f"[{table}.env]"] + [f"{_toml_str(k) if not re.fullmatch(r'[A-Za-z0-9_-]+', k) else k} = {_toml_str(v)}"
                                           for k, v in spec.env.items()]
    return nl.join(lines) + nl


def _toml_value(raw: str):
    raw = raw.strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]                      # literal string: no escapes
    try:
        import tomllib                        # 3.11+: exact parse of anything else
        return tomllib.loads("v = " + raw)["v"]
    except Exception:  # noqa: BLE001
        return raw


def toml_get(text: str, table: str) -> dict | None:
    """The ``[table]`` entry (``env`` from ``[table.env]``). ``_stray: True``
    flags a definition outside the table (inline or dotted) that
    ``copper install`` will replace."""
    lines, own, stray, keys = _toml_scan(text, table)
    if not own and not stray:
        return None
    out: dict = {}
    for sec, key, span in keys:
        val = _KEYPATH.sub("", lines[span[0]], count=1) + "".join(lines[k] for k in span[1:])
        val = re.sub(r"\s+#[^\"'\n]*$", "", val.strip())
        if sec == table and "." not in key:
            out[key] = _toml_value(val)
        elif sec == table + ".env" or (sec == table and key.startswith("env.")):
            out.setdefault("env", {})[key.split(".", 1)[1] if sec == table else key] = _toml_value(val)
    if stray:
        out["_stray"] = True
    return out


# ── install / uninstall ───────────────────────────────────────────────────
@dataclass
class Change:
    client: str
    path: str
    action: str              # added | updated | unchanged | removed | absent | error | cli
    diff: str = ""
    detail: str = ""
    created: str = ""        # "file" | "dir" when this install created the file (and its folder)

    def to_dict(self):
        d = {"client": self.client, "path": self.path, "action": self.action}
        if self.detail:
            d["detail"] = self.detail
        return d


def _norm_entry(e) -> dict | None:
    """Comparable form of a server entry: clients add defaults on their own
    (``claude mcp add-json`` stores ``"env": {}``), which must not make a
    repeat ``copper install`` look like a change."""
    if not isinstance(e, dict):
        return e
    d = {k: v for k, v in e.items() if not (k == "env" and not v)}
    d["args"] = list(d.get("args") or [])
    return d


def same_entry(a, b) -> bool:
    return _norm_entry(a) == _norm_entry(b)


def _json_apply(t: Target, entry: dict | None) -> tuple[str, str, str]:
    """Returns (old, new, action) for setting/removing copper in a JSON target."""
    old = _read(t.path)
    body = old.lstrip("﻿")
    bom = "﻿" if old.startswith("﻿") else ""
    path = t.key_path + [SERVER_NAME]
    cur = jsonc.get(body, path) if body.strip() else None
    dups = jsonc.count(body, path) if cur is not None else 0
    if dups > 1:
        # JSON keeps the LAST duplicate: collapse them so the entry we write is the one the app uses
        for _ in range(dups - 1):
            body = jsonc.remove(body, path)
    if entry is None:
        if cur is None:
            return old, old, "absent"
        return old, bom + jsonc.remove(body, path), "removed"
    if dups <= 1 and same_entry(cur, entry):
        return old, old, "unchanged"
    new = bom + jsonc.set_value(body, path, entry)
    if not old.strip():
        new = new if new.endswith("\n") else new + "\n"
    jsonc.parse(new.lstrip("﻿"))  # must still parse
    return old, new, "updated" if cur is not None else "added"


def _toml_apply(t: Target, spec: ServerSpec | None) -> tuple[str, str, str]:
    old = _read(t.path)
    table = f"mcp_servers.{SERVER_NAME}"
    cur = toml_get(old, table)
    nl = "\r\n" if "\r\n" in old else "\n"
    if spec is None:
        if cur is None:
            return old, old, "absent"
        return old, toml_remove(old, table), "removed"
    want = {**spec.entry(), **CODEX_EXTRA}
    if cur is not None and not cur.get("_stray"):
        mine = {k: cur[k] for k in _TOML_OWNED if k in cur}
        if spec.env:
            mine["env"] = cur.get("env")
        if same_entry(mine, want):
            return old, old, "unchanged"
    # Keep what the user added to our table (enabled = false, env_vars, [mcp_servers.copper.env] …).
    lines, own, _stray, keys = _toml_scan(old, table)
    kept_keys = [k for sec, key, span in keys if sec == table and key.split(".")[0] not in _TOML_OWNED
                 for k in span]
    kept_tail, cur_sec = [], None
    for i in own:
        m = _HDR.match(lines[i])
        if m:
            cur_sec = _toml_key(m.group(1))
        if cur_sec and cur_sec != table and not (spec.env and cur_sec == table + ".env"):
            kept_tail.append(i)
    block = toml_block(table, spec, CODEX_EXTRA, nl)
    if kept_keys:
        block += "".join(lines[i] if lines[i].endswith("\n") else lines[i] + nl for i in kept_keys)
    if kept_tail:
        tail = "".join(lines[i] if lines[i].endswith("\n") else lines[i] + nl for i in kept_tail)
        block += nl + tail.strip("\r\n") + nl
    base = toml_remove(old, table).rstrip("\r\n")
    new = (base + nl + nl if base else "") + block
    _toml_check(old, new, table, spec)
    return old, new, "updated" if cur is not None else "added"


def _toml_check(old: str, new: str, table: str, spec: ServerSpec):
    """Never write a config.toml Codex cannot load (verified with tomllib on 3.11+)."""
    try:
        import tomllib
    except ImportError:            # Python 3.10: best effort
        return
    try:
        tomllib.loads(old.lstrip("﻿"))
    except Exception:  # noqa: BLE001
        old_ok = False
    else:
        old_ok = True
    try:
        d = tomllib.loads(new.lstrip("﻿"))
    except Exception as e:  # noqa: BLE001
        if old_ok:
            raise ConfigFileError(f"refusing to write {table}: the merged config.toml would not parse ({e}); "
                                  "add the block from `copper config --client codex` by hand") from None
        raise ConfigFileError(f"config.toml is not valid TOML ({e}); fix it (or remove it), then run `copper install` again") from None
    got = d
    for k in table.split("."):
        got = got.get(k, {}) if isinstance(got, dict) else {}
    if got.get("command") != spec.command or list(got.get("args") or []) != list(spec.args):
        raise ConfigFileError(f"merge check failed for [{table}] — add the block from `copper config --client codex` by hand")


def _claude_cli(args: list[str], timeout=60) -> subprocess.CompletedProcess | None:
    exe = _which("claude")
    if not exe:
        return None
    cmd = [exe, *args]
    if IS_WIN and exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None


def _err(e: Exception) -> str:
    if isinstance(e, ConfigFileError):
        return str(e)
    if isinstance(e, (jsonc.JsoncError, json.JSONDecodeError)):
        return f"config is not valid JSON ({e}) — fix or delete it; Copper never overwrites a file it cannot parse"
    return f"{type(e).__name__}: {e}"


def register(client: Client, spec: ServerSpec, dry_run=False) -> list[Change]:
    out: list[Change] = []
    entry = client.shape(spec)
    for t in client.targets():
        try:
            if t.fmt == "toml":
                old, new, action = _toml_apply(t, spec)
            else:
                old, new, action = _json_apply(t, entry)
            ch = Change(client.id, str(t.path), action, _diff(t.path, old, new) if new != old else "", t.note)
            if dry_run or action == "unchanged":
                out.append(ch)
                continue
            if t.fmt == "claude-cli" and _which("claude"):
                # the official CLI handles ~/.claude.json locking while Claude Code runs
                _claude_cli(["mcp", "remove", SERVER_NAME, "-s", "user"])
                r = _claude_cli(["mcp", "add-json", SERVER_NAME, json.dumps(entry), "-s", "user"])
                if r is not None and r.returncode == 0:
                    ch.detail = "via `claude mcp add-json -s user`"
                    out.append(ch)
                    continue
                ch.detail = "claude CLI failed; merged ~/.claude.json directly" + (
                    f" ({(r.stderr or r.stdout).strip()[:200]})" if r is not None else "")
            if not t.path.exists():
                ch.created = "file" if t.path.parent.exists() else "dir"
            _write(t.path, new)
            out.append(ch)
        except Exception as e:  # noqa: BLE001
            out.append(Change(client.id, str(t.path), "error", detail=_err(e)))
    return out


def _empty_config(text: str, fmt: str) -> bool:
    """True when nothing but empty tables/objects is left (e.g. ``{"mcpServers": {}}``),
    with no comments: a file Copper created can then go."""
    body = text.lstrip("\ufeff")
    if fmt == "toml":
        return not re.sub(r"(?m)^\s*\[[^\]]*\]\s*$", "", body).strip()
    if "//" in body or "/*" in body:
        return False
    try:
        v = jsonc.to_py(body)
    except ValueError:
        return False

    def empty(x):
        return isinstance(x, dict) and all(empty(y) for y in x.values())
    return empty(v)


def _drop_created(t: Target, created: str) -> str:
    """Delete a config file (its backup, and its folder) that Copper created."""
    t.path.unlink()
    bak = Path(str(t.path) + BACKUP_SUFFIX)
    if bak.exists():
        bak.unlink()                       # a backup of Copper's own earlier content
    if created == "dir":
        try:
            t.path.parent.rmdir()          # only if empty
        except OSError:
            pass
    return "deleted the file Copper had created"


def unregister(client: Client, dry_run=False, restore_backup=False, created: dict | None = None) -> list[Change]:
    """``created`` maps config paths to "file"/"dir" for files Copper itself created
    (from the manifest); those are deleted when nothing else is left in them."""
    out: list[Change] = []
    created = created or {}
    for t in client.targets():
        try:
            bak = Path(str(t.path) + BACKUP_SUFFIX)
            if restore_backup and bak.exists():
                old = _read(t.path)
                new = _read(bak)
                ch = Change(client.id, str(t.path), "restored", _diff(t.path, old, new), f"from {bak.name}")
                if not dry_run:
                    shutil.copy2(bak, t.path)
                    bak.unlink()
                out.append(ch)
                continue
            if t.fmt == "toml":
                old, new, action = _toml_apply(t, None)
            else:
                old, new, action = _json_apply(t, None)
            ch = Change(client.id, str(t.path), action, _diff(t.path, old, new) if new != old else "")
            made = created.get(str(t.path))
            if action == "removed" and made and _empty_config(new, t.fmt):
                ch.detail = "Copper created this file; it is deleted"
                if not dry_run:
                    _drop_created(t, made)
                out.append(ch)
                continue
            if not dry_run and action == "removed":
                if t.fmt == "claude-cli" and _which("claude"):
                    r = _claude_cli(["mcp", "remove", SERVER_NAME, "-s", "user"])
                    if r is not None and r.returncode == 0:
                        ch.detail = "via `claude mcp remove -s user`"
                        out.append(ch)
                        continue
                _write(t.path, new)
            out.append(ch)
        except Exception as e:  # noqa: BLE001
            out.append(Change(client.id, str(t.path), "error", detail=_err(e)))
    return out


# ── manifest of what we registered (for `copper uninstall` / doctor) ──────
def _manifest_path() -> Path:
    return copper_home() / "clients.json"


def load_manifest() -> dict:
    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_manifest(d: dict):
    p = _manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")


def install(names=None, dry_run=False, launcher: str | None = None) -> dict:
    """Register Copper with ``names`` ('all' = every detected client)."""
    spec = server_spec(launcher)
    clients = resolve_names(names)
    changes: list[Change] = []
    for c in clients:
        changes += register(c, spec, dry_run)
    if not dry_run:
        m = load_manifest()
        for ch in changes:
            if ch.action in ("added", "updated", "unchanged"):
                prev = m.get(ch.client, {}).get(ch.path) or {}
                rec = {"version": __version__, "command": spec.command, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                made = ch.created or prev.get("created")
                if made:
                    rec["created"] = made     # Copper made this file: `copper uninstall` may delete it
                m.setdefault(ch.client, {})[ch.path] = rec
        _save_manifest(m)
    return {"server": spec.entry(), "clients": [c.id for c in clients], "dry_run": dry_run,
            "changes": [c.to_dict() for c in changes], "diffs": [c.diff for c in changes if c.diff],
            "restart": {c.id: c.restart_hint for c in clients}}


def uninstall(names=None, dry_run=False, restore_backups=False) -> dict:
    if names in (None, "all", ["all"]):
        clients = list(CLIENTS)          # remove from everything, detected or not
    else:
        clients = resolve_names(names)
    changes: list[Change] = []
    m = load_manifest()
    for c in clients:
        made = {p: r.get("created") for p, r in (m.get(c.id) or {}).items() if isinstance(r, dict) and r.get("created")}
        changes += unregister(c, dry_run, restore_backups, made)
    if not dry_run:
        for c in clients:
            m.pop(c.id, None)
        _save_manifest(m)
    # the *.copper-backup files are the user's originals: never deleted silently, always listed
    backups = sorted({str(t.path) + BACKUP_SUFFIX for c in clients for t in c.targets()
                      if Path(str(t.path) + BACKUP_SUFFIX).exists()})
    return {"clients": [c.id for c in clients], "dry_run": dry_run,
            "changes": [c.to_dict() for c in changes if c.action != "absent"],
            "diffs": [c.diff for c in changes if c.diff], "backups": backups}


# ── inspection (doctor / status) ──────────────────────────────────────────
def inspect_entry(entry: dict | None) -> tuple[str, str]:
    """('ok'|'warn'|'fail', detail) for a registered copper entry."""
    if not entry:
        return "fail", "no copper entry"
    cmd = entry.get("command")
    if isinstance(cmd, dict):                      # legacy Zed shape {"path":..}
        cmd = cmd.get("path")
    args = entry.get("args") or []
    if not cmd:
        return "fail", "entry has no command"
    p = Path(cmd)
    if p.is_absolute():
        if not p.exists():
            return "fail", f"command does not exist: {cmd}"
    elif not shutil.which(cmd):
        return "warn", f"'{cmd}' is not on PATH (GUI apps may not find it) — re-run `copper install`"
    joined = " ".join(map(str, args))
    m = re.search(r"sys\.path\.insert\(0, (['\"])(.+?)\1\)", joined)
    if m and not Path(m.group(2).replace("\\\\", "\\")).is_dir():
        return "fail", f"source checkout moved or deleted: {m.group(2)} — run `copper install` from the new location"
    if _ephemeral(str(cmd)):
        return "warn", f"{cmd} lives in uv's cache and disappears on `uv cache clean` — run `copper install` again"
    if "serve" not in joined and "copper" not in Path(cmd).name.lower() and "copper" not in joined:
        return "warn", f"unexpected args {args}"
    return "ok", f"{cmd} {joined}".strip()


def entries() -> list[dict]:
    """Every target of every client with the copper entry it holds (if any)."""
    out = []
    for c in CLIENTS:
        det = c.detected()
        for t in c.targets():
            try:
                text = _read(t.path)
                if t.fmt == "toml":
                    e = toml_get(text, f"mcp_servers.{SERVER_NAME}")
                else:
                    e = jsonc.get(text.lstrip("﻿"), t.key_path + [SERVER_NAME]) if text.strip() else None
            except (ValueError, ConfigFileError, OSError) as ex:   # JsoncError is a ValueError
                kind = "not valid JSON" if isinstance(ex, ValueError) else "unreadable"
                out.append({"client": c.id, "name": c.name, "detected": det, "path": str(t.path),
                            "exists": True, "entry": None, "status": "fail",
                            "detail": f"config is {kind}: {ex}"})
                continue
            if e and e.get("_stray"):
                st, detail = "fail", "copper is defined inline/dotted, not as [mcp_servers.copper] — run `copper install --client codex`"
            else:
                st, detail = inspect_entry(e) if e else ("absent", "not registered")
            out.append({"client": c.id, "name": c.name, "detected": det, "path": str(t.path),
                        "exists": t.path.exists(), "entry": e, "status": st, "detail": detail})
    return out


# ── one-click links ───────────────────────────────────────────────────────
def cursor_deeplink(spec: ServerSpec | None = None) -> str:
    import base64
    spec = spec or ServerSpec("npx", ["-y", "copper-kicad", "serve"])     # the npm launcher; no PyPI package
    cfg = base64.b64encode(json.dumps(spec.entry(), separators=(",", ":")).encode()).decode()
    return f"cursor://anysphere.cursor-deeplink/mcp/install?name={SERVER_NAME}&config={cfg}"


def vscode_link(spec: ServerSpec | None = None) -> str:
    from urllib.parse import quote
    spec = spec or ServerSpec("npx", ["-y", "copper-kicad", "serve"])
    cfg = {"name": SERVER_NAME, **spec.entry()}
    return "vscode:mcp/install?" + quote(json.dumps(cfg, separators=(",", ":")))
