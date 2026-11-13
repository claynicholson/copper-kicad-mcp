"""KiCad environment discovery: installs, kicad-cli, bundled python, config
dir, path variables and (global + project) library tables.

Everything is cached per-process but cheap to refresh (``refresh()``).
Which install is *active* is decided by ``compat.select`` (newest stable
KiCad 10 that reads the open project; a nightly never outranks a stable
release unless pinned) and every kicad-cli call goes through :func:`run`,
the single place where file-format and flag compatibility are checked.
"""
from __future__ import annotations

import contextvars
import dataclasses
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import sexpr
from ..mcp.registry import ToolError
from . import compat

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


@dataclass
class Install:
    version: str            # "10.0", "9.0", "nightly"
    root: Path              # install root
    cli: Path | None        # kicad-cli executable
    python: Path | None     # KiCad's bundled python (has pcbnew) if any
    share: Path | None      # .../share/kicad (symbols, footprints, templates)
    full_version: str = ""  # "10.0.6", "10.99.0-3904-gabcdef12"
    ver: compat.KiVer | None = None     # parsed full_version; the only thing version gates compare
    reason: str = ""        # why this install is the active one (set on the copy kicad() returns)
    cli_argv: list | None = None        # how to launch kicad-cli when it is not a plain executable (flatpak)

    @property
    def major(self) -> int:
        if self.ver:
            return self.ver.major
        m = re.match(r"(\d+)", self.full_version or self.version)
        return int(m.group(1)) if m else 0

    @property
    def series(self) -> str:
        return self.ver.series if self.ver else ""

    def to_dict(self):
        d = {"version": self.full_version or self.version, "root": str(self.root),
             "kicad_cli": str(self.cli) if self.cli else None,
             "python": str(self.python) if self.python else None,
             "share": str(self.share) if self.share else None}
        if self.reason:
            d["reason"] = self.reason
        return d


def _ver_key(v: str):
    nums = [int(x) for x in re.findall(r"\d+", v)] or [0]
    return nums + [0] * (4 - len(nums))


def _cli_version(cli: Path) -> str:
    """Raw first line of ``kicad-cli version`` ("10.0.6", "10.99.0-3904-gabcdef12"); "" when it cannot run."""
    # Every kicad-cli run writes <config>/<major.minor>/kicad.json + kicad_common.json. Asking for the
    # version must not do that to the user's settings: a config folder that Copper's probe created makes
    # KiCad skip its own "import settings from a previous version" offer on first start, and re-saves a
    # kicad_common.json the user already has. So the probe gets a throw-away config root.
    import tempfile
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="copper-kicad-probe-")
        out = subprocess.run([str(cli), "version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW, env=dict(os.environ, KICAD_CONFIG_HOME=tmp)).stdout.strip()
        return out.splitlines()[0].strip() if out else ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


_NO_WINDOW = 0x08000000 if IS_WIN else 0


def _candidates() -> list[Install]:
    out: list[Install] = []
    env_cli = os.environ.get("COPPER_KICAD_CLI") or os.environ.get("KICAD_CLI")
    if env_cli and Path(env_cli).exists():
        p = Path(env_cli).resolve()
        root = p.parent.parent if p.parent.name.lower() == "bin" else p.parent
        out.append(_mk("env", root, p))
    if IS_WIN:
        bases = []
        for pf in filter(None, [os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
                                os.environ.get("ProgramFiles(x86)"), r"C:\Program Files"]):
            bases += [Path(pf) / "KiCad", Path(pf) / "KiCad Copper"]
        bases.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad")
        seen = set()
        for b in bases:
            if not b.is_dir() or str(b).lower() in seen:
                continue
            seen.add(str(b).lower())
            if (b / "bin" / "kicad-cli.exe").exists():
                out.append(_mk(b.name, b, b / "bin" / "kicad-cli.exe"))
            for ver in sorted(os.listdir(b), key=_ver_key, reverse=True):
                root = b / ver
                cli = root / "bin" / "kicad-cli.exe"
                if cli.exists():
                    out.append(_mk(ver, root, cli))
    elif IS_MAC:
        for app in [Path("/Applications/KiCad/KiCad.app"), Path("/Applications/KiCad.app"),
                    Path.home() / "Applications/KiCad/KiCad.app"]:
            cli = app / "Contents/MacOS/kicad-cli"
            if cli.exists():
                out.append(_mk("mac", app, cli))
        for d in Path("/Applications").glob("KiCad*/KiCad.app"):
            cli = d / "Contents/MacOS/kicad-cli"
            if cli.exists():
                out.append(_mk(d.parent.name, d, cli))
    else:
        for cli in [shutil.which("kicad-cli"), "/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
                    "/usr/bin/kicad-cli-nightly"]:
            if cli and Path(cli).exists():
                out.append(_mk("system", Path(cli).parent.parent, Path(cli)))
        fp = shutil.which("flatpak")
        if fp:
            try:
                r = subprocess.run([fp, "info", "org.kicad.KiCad"], capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    out.append(Install("flatpak", Path("/var/lib/flatpak/app/org.kicad.KiCad"), None, None, None))
            except Exception:  # noqa: BLE001
                pass
    w = shutil.which("kicad-cli")
    if w and not any(i.cli and Path(w).resolve() == i.cli.resolve() for i in out):
        out.append(_mk("path", Path(w).parent.parent, Path(w)))
    return out


def _mk(ver: str, root: Path, cli: Path) -> Install:
    py = None
    share = None
    if IS_WIN:
        p = root / "bin" / "python.exe"
        py = p if p.exists() else None
        s = root / "share" / "kicad"
        share = s if s.exists() else None
    elif IS_MAC:
        fw = root / "Contents/Frameworks/Python.framework/Versions"
        if fw.exists():
            for v in sorted(fw.iterdir(), reverse=True):
                if (v / "bin" / "python3").exists():
                    py = v / "bin" / "python3"
                    break
        s = root / "Contents/SharedSupport"
        share = s if s.exists() else None
    else:
        for s in [root / "share" / "kicad", Path("/usr/share/kicad"), Path("/usr/local/share/kicad")]:
            if s.exists():
                share = s
                break
        p = shutil.which("python3")
        py = Path(p) if p else None
    return Install(ver, root, cli, py, share)


@functools.lru_cache(maxsize=1)
def installs() -> list[Install]:
    """Every KiCad found, newest first (a pre-release sorts below the release of the same number)."""
    found = _candidates()
    uniq: dict[str, Install] = {}
    for i in found:
        key = str(i.cli.resolve()).lower() if i.cli else str(i.root)
        if key not in uniq:
            if i.cli:
                raw = _cli_version(i.cli) or _cli_version(i.cli)     # one retry: a cold start can time out once
                i.ver = compat.parse_version(raw)
                i.full_version = str(i.ver) if i.ver else raw
            uniq[key] = i
    lst = list(uniq.values())
    lst.sort(key=lambda i: (compat.newest_key(i.ver), _ver_key(i.full_version or i.version)), reverse=True)
    return lst


# Formats of the project the current tool call works on: (("pcb", 20241229), ("sch", 20250114)).
# Set by project.resolve() so that kicad() can pick the install that reads the project without
# every one of its call sites having to pass a project along.
_PROJECT: contextvars.ContextVar = contextvars.ContextVar("copper_project_fmt", default=None)
_select_memo: dict = {}


def set_project_format(stamps: tuple | None) -> None:
    _PROJECT.set(tuple(stamps) if stamps else None)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def kicad() -> Install:
    """The active install: ``compat.select`` over :func:`installs` for the
    current project's formats. ``.reason`` says why it was chosen."""
    lst = [i for i in installs() if i.cli]
    if not lst:
        raise ToolError("KiCad was not found on this computer.", code="KICAD_NOT_FOUND",
                        fix="Install KiCad 10 from https://www.kicad.org/download/ (or set COPPER_KICAD_CLI to kicad-cli's path), then run `copper doctor`.")
    pin_cli = os.environ.get("COPPER_KICAD_CLI") or os.environ.get("KICAD_CLI") or ""
    pin = os.environ.get("COPPER_KICAD_VERSION") or ""
    allow = _truthy("COPPER_KICAD_ALLOW_PRERELEASE")
    key = (id(lst[0]), len(lst), pin_cli, pin, _PROJECT.get(), allow)
    hit = _select_memo.get(key)
    if hit is not None:
        return hit
    chosen, why = None, ""
    if pin_cli and Path(pin_cli).exists():
        want = str(Path(pin_cli).resolve()).lower()
        for i in lst:
            if str(i.cli.resolve()).lower() == want:
                chosen, why = i, "pinned by COPPER_KICAD_CLI"
                break
    if chosen is None:
        chosen, why = compat.select(lst, pin or None, _PROJECT.get(), allow)
    out = dataclasses.replace(chosen, reason=why)
    if len(_select_memo) > 64:
        _select_memo.clear()
    _select_memo[key] = out
    return out


def refresh():
    installs.cache_clear()
    _select_memo.clear()
    _config_memo.clear()


# ── config dir + path variables ───────────────────────────────────────────
def config_root() -> Path:
    # KiCad treats KICAD_CONFIG_HOME as the root and appends "<major>.<minor>" itself
    if os.environ.get("KICAD_CONFIG_HOME"):
        return Path(os.environ["KICAD_CONFIG_HOME"])
    if IS_WIN:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "kicad"
    if IS_MAC:
        return Path.home() / "Library/Preferences/kicad"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad"


_config_memo: dict = {}


def config_dir() -> Path:
    """Config dir of the active KiCad: ``<config_root>/<major.minor>`` (created lazily by KiCad).

    Never export this as KICAD_CONFIG_HOME: KiCad appends ``<major.minor>``
    to that variable itself, so doing it makes KiCad use (and create) an
    empty ``…/10.0/10.0``."""
    root = config_root()
    try:
        series = kicad().series
    except ToolError:
        series = ""
    key = (str(root), series)
    if key not in _config_memo:
        _config_memo[key] = _config_dir(root, series)
    return _config_memo[key]


def _config_dir(root: Path, series: str) -> Path:
    dirs = []
    if root.exists():
        try:
            dirs = [d for d in root.iterdir() if d.is_dir() and re.fullmatch(r"\d+\.\d+", d.name)]
        except OSError:
            dirs = []
    if series:
        if (root / series).exists() or not dirs:
            return root / series
        # A pre-release may name its folder after the release it is heading for (10.99 → 11.0; unverified):
        # if <root>/<series> is missing and exactly one folder is newer than the newest stable series, use it.
        v = compat.parse_version(series + ".0")
        if v is not None and v.nightly:
            newer = [d for d in dirs if _ver_key(d.name) > _ver_key(f"{v.major}.0") and d.name != series]
            if len(newer) == 1:
                return newer[0]
        return root / series
    if dirs:
        return max(dirs, key=lambda d: _ver_key(d.name))
    return root / "10.0"


def documents_dir() -> Path:
    if os.environ.get("KICAD_DOCUMENTS_HOME"):
        return Path(os.environ["KICAD_DOCUMENTS_HOME"])
    if os.environ.get("COPPER_CONFIG_HOME_OVERRIDE"):
        # the sandbox (tests, `copper setup --demo`): SHGetFolderPathW below ignores USERPROFILE and would
        # name the real Documents folder
        return Path(os.environ["COPPER_CONFIG_HOME_OVERRIDE"]) / "Documents"
    if IS_WIN:
        try:
            import ctypes.wintypes
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)
            if buf.value:
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Documents"


def user_data_dir() -> Path:
    """``<Documents>/KiCad`` (KICAD_DOCUMENTS_HOME, when set, already is that folder)."""
    d = documents_dir()
    return d if os.environ.get("KICAD_DOCUMENTS_HOME") else d / "KiCad"


def path_vars(project_dir: Path | None = None) -> dict[str, str]:
    """KiCad path substitution variables (${KICAD10_SYMBOL_DIR}, ${KIPRJMOD}, …).

    A nightly's library tables may use either the current major's names
    (KICAD10_*) or the next one's (KICAD11_*), so both resolve."""
    v: dict[str, str] = {}
    try:
        inst = kicad()
        major = inst.major or 10
        majors = sorted({major, inst.ver.effective_major if inst.ver else major, 8, 9, 10, 11})
        share = Path(os.environ["KICAD_STOCK_DATA_HOME"]) if os.environ.get("KICAD_STOCK_DATA_HOME") else inst.share
        if share:
            for suffix, sub in [("SYMBOL_DIR", "symbols"), ("FOOTPRINT_DIR", "footprints"),
                                ("3DMODEL_DIR", "3dmodels"), ("TEMPLATE_DIR", "template")]:
                for mj in majors:
                    v.setdefault(f"KICAD{mj}_{suffix}", str(share / sub))
        docs = user_data_dir() / (inst.series or f"{major}.0")
        for mj in majors:
            v.setdefault(f"KICAD{mj}_3RD_PARTY", str(docs / "3rdparty"))
        v.setdefault("KICAD_USER_TEMPLATE_DIR", str(docs / "template"))
    except ToolError:
        pass
    try:
        common = json.loads((config_dir() / "kicad_common.json").read_text(encoding="utf-8"))
        for k, val in ((common.get("environment") or {}).get("vars") or {}).items():
            if val:
                v[k] = val
    except (OSError, ValueError):
        pass
    for k, val in os.environ.items():
        if re.match(r"KICAD\d*_", k):
            v[k] = val
    if project_dir:
        v["KIPRJMOD"] = str(project_dir)
        pro = next(Path(project_dir).glob("*.kicad_pro"), None)
        if pro:
            try:
                pj = json.loads(pro.read_text(encoding="utf-8"))
                for k, val in (pj.get("text_variables") or {}).items():
                    v.setdefault(k, val)
            except (OSError, ValueError):
                pass
    return v


def expand(uri: str, variables: dict[str, str]) -> str:
    def rep(m):
        return variables.get(m.group(1), m.group(0))
    out = uri
    for _ in range(4):
        new = re.sub(r"\$\{([^}]+)\}", rep, out)
        new = re.sub(r"\$\(([^)]+)\)", rep, new)
        if new == out:
            break
        out = new
    return out


# ── library tables ────────────────────────────────────────────────────────
@dataclass
class Lib:
    name: str
    type: str
    uri: str
    path: str
    scope: str  # "global" | "project" | "template"
    descr: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.path) and "${" not in self.path and os.path.exists(self.path)


def _parse_table(file: Path, scope: str, variables: dict, depth=0) -> list[Lib]:
    try:
        tree = sexpr.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    libs = []
    for e in sexpr.find_all(tree, "lib"):
        name = sexpr.value(e, "name", "")
        typ = sexpr.value(e, "type", "KiCad")
        uri = sexpr.value(e, "uri", "")
        disabled = sexpr.find(e, "disabled") is not None
        if disabled:
            continue
        p = expand(str(uri), variables)
        if str(typ).lower() == "table" and depth < 3:
            libs += _parse_table(Path(p), scope, variables, depth + 1)
            continue
        libs.append(Lib(str(name), str(typ), str(uri), os.path.normpath(p) if p else "", scope,
                        str(sexpr.value(e, "descr", "") or "")))
    return libs


def lib_tables(kind: str, project_dir: Path | None = None) -> list[Lib]:
    """kind: 'sym' or 'fp'. Project table entries shadow global ones."""
    assert kind in ("sym", "fp")
    variables = path_vars(project_dir)
    fname = f"{kind}-lib-table"
    libs: list[Lib] = []
    if project_dir and (Path(project_dir) / fname).exists():
        libs += _parse_table(Path(project_dir) / fname, "project", variables)
    g = config_dir() / fname
    if g.exists():
        libs += _parse_table(g, "global", variables)
    else:
        try:
            inst = kicad()
            if inst.share and (inst.share / "template" / fname).exists():
                libs += _parse_table(inst.share / "template" / fname, "template", variables)
        except ToolError:
            pass
    seen, out = set(), []
    for lib in libs:
        if lib.name not in seen:
            seen.add(lib.name)
            out.append(lib)
    return out


def find_lib(kind: str, name: str, project_dir: Path | None = None) -> Lib:
    for lib in lib_tables(kind, project_dir):
        if lib.name == name:
            if not lib.exists:
                raise ToolError(f"library '{name}' is in the {lib.scope} {kind}-lib-table but its file is missing: {lib.path}",
                                code="LIB_MISSING", fix="library(action='repair') to re-point the tables at your KiCad install")
            return lib
    raise ToolError(f"{'symbol' if kind == 'sym' else 'footprint'} library '{name}' not found in any lib table",
                    code="LIB_NOT_FOUND",
                    fix=f"library(action='search_{'symbols' if kind == 'sym' else 'footprints'}', query=...) to find the right Library:Name id")


def run(args: list[str], timeout: int = 300, cwd: str | None = None, check=False) -> subprocess.CompletedProcess:
    """Run kicad-cli with args (without the executable). The one launcher.

    Before the call: input design files newer than the active KiCad reads are
    refused (FORMAT_TOO_NEW; kicad-cli would fail without a reason, or drop a
    too-new sub-sheet silently), and optional flags the binary's own --help
    does not list are dropped (``result.copper_dropped`` names them and a
    one-time note reaches the tool result). KICAD_CONFIG_HOME is passed
    through untouched: KiCad appends '<major.minor>' to it itself. stdin is
    detached so the child can never block on the MCP server's stdin pipe."""
    inst = kicad()
    if inst.cli is None:
        raise ToolError("kicad-cli is not available for this KiCad install (flatpak?)", code="NO_CLI",
                        fix="install KiCad natively or set COPPER_KICAD_CLI")
    compat.require_working(inst)
    args, dropped = compat.prepare_args(inst, [str(a) for a in args])
    if args[:2] == ["pcb", "drc"]:
        compat.advise_zone_fill(inst, args[-1])
    env = dict(os.environ)
    try:
        r = subprocess.run([*(inst.cli_argv or [str(inst.cli)]), *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, cwd=cwd, env=env, creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise ToolError(f"kicad-cli {' '.join(args[:3])} timed out after {timeout}s", code="TIMEOUT",
                        fix="the design may be very large — retry, or run the same check in the KiCad GUI")
    r.copper_dropped = dropped
    if check and r.returncode not in (0,):
        raise ToolError(f"kicad-cli {' '.join(args[:3])} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-800:]}",
                        code="KICAD_CLI_FAILED")
    return r


def kicad_python(inst: Install | None = None) -> Path | None:
    """The ACTIVE install's interpreter that can ``import pcbnew`` (KiCad <= 10), or None.

    Never another install's: a board must be edited by the same KiCad whose
    kicad-cli checks and plots it. On Linux every install shares the system
    python3, so the pcbnew worker additionally verifies ``pcbnew.Version()``
    against the active series before it loads a board (ENGINE_MISMATCH)."""
    try:
        inst = inst or kicad()
    except ToolError:
        return None
    try:
        return inst.python if inst.python and inst.python.exists() else None
    except OSError:
        return None


def summary() -> dict:
    lst = installs()
    d = {"installs": [i.to_dict() for i in lst], "config_dir": str(config_dir())}
    try:
        act = kicad()
        d["active"] = act.to_dict()
        # warnings only: informational notes stay in `copper doctor` so the status payload stays small
        adv = [{k: a[k] for k in ("id", "severity", "text")} for a in compat.advisories(act.ver) if a["severity"] == "warn"]
        try:    # no network here: only what `copper doctor` already cached
            lat = compat.latest_release(cached_only=True)
            if lat and act.ver and not act.ver.prerelease and lat.t > act.ver.t:
                adv.insert(0, {"id": "update-available", "severity": "info",
                               "text": f"KiCad {lat} is available (installed: {act.ver})"})
        except Exception:  # noqa: BLE001
            pass
        if adv:
            d["advisories"] = adv
        compat.require_working(act)       # an install whose kicad-cli gave no version is not usable: say so
    except ToolError as e:
        if e.code == "KICAD_CLI_BROKEN":
            d["problem"] = e.to_dict()
            return d
        d["active"] = None
        d["problem"] = e.to_dict()
    return d
