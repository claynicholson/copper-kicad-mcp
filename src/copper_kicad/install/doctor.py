"""`copper doctor` — every check that decides whether Copper will work.

Used by the CLI (``copper doctor [--json]``), the installer's Finish page and
the ``copper_status`` tool (``run(fast=True, network=False)`` is cheap enough
to call on every status request).

Each check returns ``Check(id, label, status, detail, fix)`` where status is
``ok`` | ``warn`` | ``fail`` | ``skip``. ``fail`` means a core feature will not
work; ``warn`` means an optional feature is unavailable.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import __version__

IS_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if IS_WIN else 0

NETWORK_ENDPOINTS = [
    ("jlcsearch", "https://jlcsearch.tscircuit.com/", "LCSC/JLC part search"),
    ("jlcpcb", "https://jlcpcb.com/", "JLCPCB assembly stock"),
]
# parts_import tries these in order (parts/easyeda.py fetch); one working source is enough.
try:
    from ..parts.easyeda import EE_MIRROR as _EE_MIRROR, EE_URL as _EE_URL
except Exception:  # noqa: BLE001
    _EE_URL = "https://easyeda.com/api/products/{lcsc}/components?version=6.4.19.5"
    _EE_MIRROR = "https://jlcsearch.tscircuit.com/api/easyeda_components/{lcsc}"
EASYEDA_SOURCES = [("easyeda", _EE_URL.format(lcsc="C25804")), ("mirror", _EE_MIRROR.format(lcsc="C25804"))]


@dataclass
class Check:
    id: str
    label: str
    status: str               # ok | warn | fail | skip
    detail: str = ""
    fix: str | None = None
    group: str = "core"
    ms: int = 0

    def to_dict(self):
        d = asdict(self)
        if not d["fix"]:
            d.pop("fix")
        return d


def _timed(fn):
    def wrap(*a, **k):
        t = time.perf_counter()
        r = fn(*a, **k)
        ms = int((time.perf_counter() - t) * 1000)
        for c in (r if isinstance(r, list) else [r]):
            c.ms = c.ms or ms
        return r
    return wrap


# ── individual checks ─────────────────────────────────────────────────────
def check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 10)
    return Check("python", "Python", "ok" if ok else "fail",
                 f"{platform.python_implementation()} {platform.python_version()} ({sys.executable})",
                 None if ok else "Copper needs Python 3.10+; reinstall with `uv tool install --python 3.12 copper-kicad`")


def check_pillow() -> Check:
    try:
        import PIL
        from PIL import Image  # noqa: F401
        return Check("pillow", "Pillow (image rendering)", "ok", f"Pillow {PIL.__version__}")
    except Exception as e:  # noqa: BLE001
        return Check("pillow", "Pillow (image rendering)", "fail", f"{type(e).__name__}: {e}",
                     "install Pillow into Copper's environment: `uv tool install --reinstall copper-kicad`")


def check_copper() -> Check:
    from .clients import _installed_in_site_packages, _package_dir
    where = _package_dir()
    return Check("copper", "Copper", "ok", f"copper-kicad {__version__} — "
                 f"{'installed' if _installed_in_site_packages() else 'source checkout'} at {where}")


@_timed
def check_kicad(fast: bool = False) -> list[Check]:
    """One row per installed KiCad (kicad/compat_report.py): version, channel,
    which one is active and why, formats, PCB engine, kicad-cli capabilities."""
    from ..kicad import env
    out = []
    try:
        inst = env.kicad()
    except Exception as e:  # noqa: BLE001
        fix = getattr(e, "fix", None) or "install KiCad 10 from https://www.kicad.org/download/"
        return [Check("kicad", "KiCad", "fail", str(getattr(e, "message", e)), fix)]
    try:
        from ..kicad import compat_report
        out += [Check(**r) for r in compat_report.checks(compat_report.table(probe=not fast))]
    except Exception as e:  # noqa: BLE001 — the table is a report; never lose the basic row over it
        out.append(Check("kicad", "KiCad", "ok" if inst.major >= 10 else "warn",
                         f"KiCad {inst.full_version or inst.version} at {inst.root} (compatibility table failed: {e})"))
    # kicad-cli actually runs
    t = time.perf_counter()
    # a throw-away config root: any kicad-cli run creates <config>/<ver>/kicad.json + kicad_common.json, and a
    # health check must not create (or re-save) the user's KiCad settings
    import shutil as _sh
    import tempfile
    probe_cfg = tempfile.mkdtemp(prefix="copper-kicad-probe-")
    try:
        r = subprocess.run([str(inst.cli), "version"], capture_output=True, text=True, timeout=60,
                           creationflags=_NO_WINDOW, env=dict(os.environ, KICAD_CONFIG_HOME=probe_cfg))
        ok = r.returncode == 0 and r.stdout.strip()
        out.append(Check("kicad_cli", "kicad-cli runs", "ok" if ok else "fail",
                         f"{inst.cli} → {r.stdout.strip() or r.stderr.strip()[:200]}",
                         None if ok else "reinstall KiCad, or set COPPER_KICAD_CLI to a working kicad-cli",
                         ms=int((time.perf_counter() - t) * 1000)))
    except Exception as e:  # noqa: BLE001
        out.append(Check("kicad_cli", "kicad-cli runs", "fail", f"{type(e).__name__}: {e}",
                         "reinstall KiCad, or set COPPER_KICAD_CLI to a working kicad-cli"))
    finally:
        _sh.rmtree(probe_cfg, ignore_errors=True)
    return out


@_timed
def check_pcbnew() -> Check:
    from ..kicad import env
    try:
        py = env.kicad_python()
    except Exception:  # noqa: BLE001
        py = None
    if not py:
        return Check("pcbnew", "KiCad Python + pcbnew", "warn", "the active KiCad has no Python with pcbnew",
                     "PCB editing needs it; install KiCad 10.0.x (the Windows/macOS installers bundle Python; "
                     "KiCad 11 no longer has it)", "pcb")
    try:
        r = subprocess.run([str(py), "-c", "import pcbnew, sys; print(pcbnew.Version()); print(sys.version.split()[0])"],
                           capture_output=True, text=True, timeout=90, creationflags=_NO_WINDOW,
                           env={k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")})
        if r.returncode == 0:
            lines = r.stdout.strip().splitlines()
            return Check("pcbnew", "KiCad Python + pcbnew", "ok",
                         f"pcbnew {lines[0] if lines else '?'} on Python {lines[1] if len(lines) > 1 else '?'} ({py})", group="pcb")
        return Check("pcbnew", "KiCad Python + pcbnew", "fail", (r.stderr or r.stdout).strip()[-300:],
                     "repair the KiCad install (pcbnew must import in KiCad's python)", "pcb")
    except subprocess.TimeoutExpired:
        return Check("pcbnew", "KiCad Python + pcbnew", "warn", "import pcbnew timed out (90 s)", None, "pcb")
    except Exception as e:  # noqa: BLE001
        return Check("pcbnew", "KiCad Python + pcbnew", "fail", f"{type(e).__name__}: {e}", None, "pcb")


def check_lib_tables() -> Check:
    try:
        from .kicad_setup import lib_tables_status
        s = lib_tables_status()
    except Exception as e:  # noqa: BLE001
        return Check("lib_tables", "Global library tables", "skip", f"{type(e).__name__}: {e}")
    sym, fp = s["sym"], s["fp"]
    ok = sym["exists"] and fp["exists"] and sym["libs"] and fp["libs"]
    detail = f"{sym['libs']} symbol libs, {fp['libs']} footprint libs in {s['config_dir']}"
    if ok:
        # spot-check that entries resolve to real files
        try:
            from ..kicad import env
            libs = env.lib_tables("sym")
            missing = [lib.name for lib in libs if lib.type.lower() == "kicad" and not lib.exists]
            if libs and len(missing) > len(libs) // 2:
                return Check("lib_tables", "Global library tables", "fail",
                             f"{len(missing)}/{len(libs)} symbol libraries point at missing files (e.g. {missing[0]})",
                             "run `copper setup` (repairs the tables) or library(action='repair')")
        except Exception:  # noqa: BLE001
            pass
        return Check("lib_tables", "Global library tables", "ok", detail)
    missing = ", ".join(k + "-lib-table" for k in ("sym", "fp") if not s[k]["exists"] or not s[k]["libs"])
    try:
        from .kicad_setup import pending_migration
        prev = pending_migration()
    except Exception:  # noqa: BLE001
        prev = None
    if prev is not None:
        return Check("lib_tables", "Global library tables", "warn",
                     f"KiCad has not been started since the upgrade; its KiCad {prev.name} settings are not imported yet",
                     f"start KiCad once and accept 'Import settings from KiCad {prev.name}', then `copper setup`")
    try:
        from ..kicad import env
        share = env.kicad().share
        has_template = bool(share and (share / "template" / "sym-lib-table").exists())
    except Exception:  # noqa: BLE001
        has_template = False
    if has_template:
        return Check("lib_tables", "Global library tables", "warn",
                     f"missing in {s['config_dir']} ({missing}) — KiCad's first-run setup never ran; Copper reads "
                     "KiCad's default tables meanwhile, but the KiCad GUI will not see Copper-added libraries",
                     "run `copper setup` (creates them from KiCad's defaults)")
    return Check("lib_tables", "Global library tables", "fail", f"missing: {missing}",
                 "run `copper setup` (creates them from KiCad's defaults)")


def check_api() -> Check:
    try:
        from .kicad_setup import api_status
        s = api_status()
    except Exception as e:  # noqa: BLE001
        return Check("kicad_api", "KiCad IPC API", "skip", f"{type(e).__name__}: {e}", group="optional")
    if s.get("enabled"):
        return Check("kicad_api", "KiCad IPC API", "ok", f"enabled in {s['path']} (live PCB edits while KiCad is open)",
                     group="optional")
    return Check("kicad_api", "KiCad IPC API", "warn", "disabled — Copper edits closed files only",
                 "close KiCad, then `copper kicad-api enable` (or Preferences → Plugins → Enable KiCad API)", "optional")


_BLOCKED = (401, 403, 407, 429)   # the host answered but refuses us: the feature will not work


def _headers(name: str) -> dict:
    """The headers the real client sends, so a probe sees what an import would."""
    h = {"User-Agent": f"copper-kicad/{__version__} doctor"}
    if name.startswith("easyeda"):
        try:
            from ..parts import common as pc
            h["User-Agent"] = pc.UA_BROWSER
        except Exception:  # noqa: BLE001
            pass
        if name == "easyeda":
            h.update({"Referer": "https://easyeda.com/", "Accept": "application/json"})
    return h


def _http_code(name, url, timeout) -> tuple[int | None, str, int]:
    """(HTTP status or None, failure reason, ms)."""
    t = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers=_headers(name), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, reason = r.status, ""
    except urllib.error.HTTPError as e:
        code, reason = e.code, ""
    except Exception as e:  # noqa: BLE001
        code, reason = None, str(getattr(e, "reason", e))
    return code, reason, int((time.perf_counter() - t) * 1000)


def _status_of(code: int | None) -> str:
    return "ok" if code is not None and code < 400 else "warn"


def _probe(name, url, purpose, timeout) -> Check:
    code, reason, ms = _http_code(name, url, timeout)
    if code is None:
        return Check(f"net_{name}", f"Network: {name}", "warn", f"unreachable ({reason}) — {purpose} will not work",
                     "check your internet connection / proxy (HTTPS_PROXY is honoured)", "network", ms)
    st = _status_of(code)
    why = " (blocked: the host refuses this client)" if code in _BLOCKED else " (server error)" if code >= 500 else ""
    return Check(f"net_{name}", f"Network: {name}", st,
                 f"HTTP {code}{why} in {ms} ms — {purpose}" + ("" if st == "ok" else " may not work"),
                 None if st == "ok" else "retry later; check proxy / VPN / firewall rules for this host", "network", ms)


def _probe_easyeda(timeout) -> Check:
    """EasyEDA symbol/footprint import tries easyeda.com, then the jlcsearch
    mirror (parts.easyeda.fetch). Report which source actually works."""
    purpose = "EasyEDA symbol/footprint import (parts_import)"
    res = {n: _http_code(n, u, timeout) for n, u in EASYEDA_SOURCES}
    ok = [n for n, (c, _r, _m) in res.items() if _status_of(c) == "ok"]
    parts = []
    for n, (c, r, m) in res.items():
        parts.append(f"{n} " + (f"HTTP {c}" + (" blocked" if c in _BLOCKED else "") if c is not None else f"unreachable ({r})")
                     + f" in {m} ms")
    ms = max(m for _c, _r, m in res.values())
    detail = "; ".join(parts) + f" — {purpose}"
    if len(ok) == len(res):
        return Check("net_easyeda", "Network: easyeda", "ok", detail, None, "network", ms)
    if ok:
        return Check("net_easyeda", "Network: easyeda", "ok", detail + f" via {ok[0]}", None, "network", ms)
    return Check("net_easyeda", "Network: easyeda", "warn", detail + " will not work",
                 "check your internet connection / proxy / VPN; parts_import needs easyeda.com or jlcsearch.tscircuit.com",
                 "network", ms)


def _config_view() -> dict:
    """Active parts providers + fab profile from the Copper config ($COPPER_HOME/config.json); never raises."""
    try:
        from .. import config
        return config.describe(None)
    except Exception as e:  # noqa: BLE001
        return {"error": "CONFIG", "message": str(e)}


def check_config() -> Check:
    c = _config_view()
    if c.get("error"):
        return Check("config", "Copper config", "fail", str(c.get("message") or c["error"]), c.get("fix"), "core")
    if c.get("problems"):
        p = c["problems"][0]
        return Check("config", "Copper config", "warn", str(p.get("message")), p.get("fix"), "core")
    files = c.get("config_files") or []
    detail = (f"parts providers: {', '.join(c.get('parts_providers') or [])}; fab profile: {c.get('fab_profile')}"
              + (f" (from {', '.join(files)})" if files else " (built-in defaults)"))
    if c.get("warnings"):
        return Check("config", "Copper config", "warn", detail + " — " + "; ".join(c["warnings"]), None, "core")
    return Check("config", "Copper config", "ok", detail, None, "core")


def check_network(timeout=4.0) -> list[Check]:
    provs = _config_view().get("parts_providers") or ["lcsc"]
    if "lcsc" not in provs:
        # the endpoints below belong to the lcsc provider; other providers (local catalogue, KiCad libraries) are offline
        return [Check("net_parts", "Network: parts", "skip",
                      f"parts providers {', '.join(provs)} do not use LCSC / JLCPCB / EasyEDA — nothing to probe", None, "network")]
    jobs = [lambda e=e: _probe(*e, timeout) for e in NETWORK_ENDPOINTS] + [lambda: _probe_easyeda(timeout)]
    with cf.ThreadPoolExecutor(len(jobs)) as ex:
        return list(ex.map(lambda f: f(), jobs))


def check_clients() -> list[Check]:
    from . import clients
    out = []
    by_client: dict[str, list[dict]] = {}
    for e in clients.entries():
        by_client.setdefault(e["client"], []).append(e)
    for cid, es in by_client.items():
        c = clients.BY_ID[cid]
        reg = [e for e in es if e["status"] != "absent"]
        detected = any(e["detected"] for e in es)
        if not reg:
            if detected:
                out.append(Check(f"client_{cid}", c.name, "warn", "installed but Copper is not registered",
                                 f"copper install --client {cid}", "clients"))
            continue
        worst = "ok"
        for e in reg:
            if e["status"] == "fail":
                worst = "fail"
            elif e["status"] == "warn" and worst == "ok":
                worst = "warn"
        detail = "; ".join(f"{Path(e['path']).name}: {e['detail']}" for e in reg)
        broken = [e for e in reg if e["entry"] is None and e["status"] == "fail"]   # unreadable / invalid file
        fix = (None if worst == "ok" else
               f"repair {broken[0]['path']} (see detail), then `copper install --client {cid}`" if broken else
               f"copper install --client {cid}")
        out.append(Check(f"client_{cid}", c.name, worst, detail, fix, "clients"))
    dup = _claude_code_plugin_duplicate(by_client.get("claude-code", []))
    if dup:
        out.append(dup)
    if not out:
        out.append(Check("clients", "AI clients", "warn", "no supported AI client found or registered",
                         "install your AI app, then `copper install --client <id>` (claude-desktop, claude-code, "
                         "codex, cursor, vscode, windsurf, zed, gemini, copilot-cli)", "clients"))
    return out


def _claude_code_plugin_duplicate(entries: list[dict]) -> Check | None:
    """The Claude Code plugin (``copper@…``) brings its own ``copper`` server;
    with a user-scope registration as well, every tool is listed twice."""
    if not any(e["status"] not in ("absent",) and e.get("entry") for e in entries):
        return None
    from . import clients
    f = clients.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        import json
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plugins = d.get("plugins", d) if isinstance(d, dict) else {}
    names = [k for k in (plugins if isinstance(plugins, dict) else {}) if str(k).split("@")[0] == "copper"]
    if not names:
        return None
    return Check("client_claude-code_dup", "Claude Code (twice)", "warn",
                 f"the {names[0]} plugin and a user-scope 'copper' server are both registered — every tool is listed twice",
                 f"keep one: `claude plugin uninstall {names[0]}` or `copper uninstall --client claude-code`", "clients")


def check_tools() -> Check:
    try:
        from .. import tools
        from ..mcp import registry
        n = len(registry.TOOLS)
        errs = dict(getattr(tools, "LOAD_ERRORS", {}) or {})
    except Exception as e:  # noqa: BLE001
        return Check("tools", "MCP tool modules", "fail", f"{type(e).__name__}: {e}",
                     "reinstall Copper: `uv tool install --reinstall copper-kicad`")
    if errs:
        first = next(iter(errs.items()))
        return Check("tools", "MCP tool modules", "fail" if n == 0 else "warn",
                     f"{n} tools loaded; failed modules: {', '.join(errs)} — {first[0]}: {first[1].splitlines()[0]}",
                     "run `copper doctor --json` and report the error; reinstall with `uv tool install --reinstall copper-kicad`")
    return Check("tools", "MCP tool modules", "ok" if n else "fail", f"{n} tools loaded")


def check_freerouting() -> Check:
    try:
        from . import freerouting
        f = freerouting.find()
    except Exception as e:  # noqa: BLE001
        return Check("freerouting", "Freerouting autorouter", "skip", f"{type(e).__name__}: {e}", group="pcb")
    where = f" in {f['tools_dir']}" if f.get("tools_dir") else ""
    if f["ok"]:
        return Check("freerouting", "Freerouting autorouter", "ok", freerouting.describe(f), group="pcb")
    if f.get("available"):
        # a fresh install: pcb_route downloads the router itself on its first run, so autorouting works
        return Check("freerouting", "Freerouting autorouter", "ok",
                     freerouting.describe(f) + "; `copper freerouting --install` gets it now (for offline use)", group="pcb")
    # group "pcb", not "optional": the advertised pcb_place → pcb_route → pcb_pour → fab_export loop needs it
    return Check("freerouting", "Freerouting autorouter", "warn",
                 f"{f.get('problem')}{where} — pcb_route cannot autoroute", f.get("fix"), "pcb")


def check_uv() -> Check:
    import shutil
    w = shutil.which("uv")
    if w:
        return Check("uv", "uv (updates)", "ok", w, group="optional")
    return Check("uv", "uv (updates)", "skip", "not on PATH — only needed for `uv tool upgrade copper-kicad`",
                 group="optional")


def check_links(launch: bool = True) -> list[Check]:
    """`copper link` state: do the registered clients really start Copper (``launch``), and the optional
    GitHub link. Never reads a token; without ``launch`` nothing is started and gh is not called."""
    out: list[Check] = []
    try:
        from ..link import apps, github
    except Exception as e:  # noqa: BLE001
        return [Check("links", "Linked accounts", "skip", f"{type(e).__name__}: {e}", group="links")]
    if launch:
        try:
            rs = apps.verify(None, timeout=20.0)
        except Exception as e:  # noqa: BLE001
            rs = []
            out.append(Check("client_launch", "Clients start Copper", "skip", f"{type(e).__name__}: {e}", group="clients"))
        bad = [r for r in rs if not r["ok"] and r["stage"] != "recognise"]
        for r in bad:
            out.append(Check(f"launch_{r['client']}", f"{r['name']} starts Copper", "fail",
                             f"registered in {Path(r['path']).name}, but the command does not answer ({r['stage']}): {r['detail']}",
                             f"copper link {r['client']}", "clients"))
        good = [r for r in rs if r["ok"]]
        if good:
            slow = max(r["initialize_ms"] or 0 for r in good)
            out.append(Check("client_launch", "Clients start Copper", "ok",
                             f"{len({r['client'] for r in good})} client(s) launch it and get {good[0]['tools']} tools "
                             f"(slowest start {slow / 1000:.1f} s)", group="clients", ms=slow))
    try:
        g = github.status(live=launch)
        if g["linked"]:
            out.append(Check("link_github", "GitHub", "ok", g["detail"], group="links"))
        elif g.get("via"):
            out.append(Check("link_github", "GitHub", "warn", g["detail"], g.get("fix") or "copper link github", "links"))
        else:
            out.append(Check("link_github", "GitHub", "skip", "not linked (optional) — copper link github", group="links"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("link_github", "GitHub", "skip", f"{type(e).__name__}: {e}", group="links"))
    return out


# ── run all ───────────────────────────────────────────────────────────────
def run(fast: bool = False, network: bool = True, clients: bool = True) -> list[Check]:
    """All checks. ``fast`` skips the pcbnew subprocess (~2-5 s)."""
    if not fast:
        try:
            from ..kicad import env
            env.refresh()          # pick up a KiCad installed/upgraded since start-up
        except Exception:  # noqa: BLE001
            pass
    checks: list[Check] = [check_python(), check_pillow(), check_copper()]
    with cf.ThreadPoolExecutor(4) as ex:
        fut_k = ex.submit(check_kicad, fast)
        fut_p = ex.submit(check_pcbnew) if not fast else None
        fut_n = ex.submit(check_network) if network else None
        fut_f = ex.submit(check_freerouting)
        checks += fut_k.result()
        try:    # newer KiCad available? one fixed GET, cached a day; cache-only when network checks are off
            from ..kicad import compat_report
            checks += [Check(**r) for r in compat_report.update_check(network=network)]
        except Exception:  # noqa: BLE001
            pass
        checks.append(check_lib_tables())
        if fut_p:
            checks.append(fut_p.result())
        checks.append(check_api())
        checks.append(check_tools())
        checks.append(check_config())
        if clients:
            checks += check_clients()
            checks += check_links(launch=not fast)
        if fut_n:
            checks += fut_n.result()
        checks.append(fut_f.result())
    checks.append(check_uv())
    return checks


def summarize(checks: list[Check]) -> dict:
    counts = {s: sum(1 for c in checks if c.status == s) for s in ("ok", "warn", "fail", "skip")}
    core_fail = [c.id for c in checks if c.status == "fail"]
    d = {"version": __version__, "healthy": not core_fail, "counts": counts, "failing": core_fail,
         "checks": [c.to_dict() for c in checks]}
    try:    # the full per-install compatibility table (CLI/JSON only; copper_status stays small)
        from ..kicad import compat_report
        d["kicad"] = compat_report.table()
    except Exception:  # noqa: BLE001
        pass
    return d


def brief() -> dict:
    """Compact, fast health digest for copper_status: no network, no pcbnew."""
    cs = run(fast=True, network=False, clients=False)
    return {"healthy": not any(c.status == "fail" for c in cs),
            "problems": [{"check": c.id, "status": c.status, "detail": c.detail, "fix": c.fix}
                         for c in cs if c.status in ("fail", "warn")]}
