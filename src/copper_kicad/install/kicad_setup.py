"""One-time KiCad preparation used by `copper setup` and the installer:

* enable KiCad's IPC scripting API (``api.enable_server`` in kicad_common.json)
  — only with the user's consent and only while KiCad is closed, because
  KiCad rewrites kicad_common.json on exit and would undo the change;
* make sure the global symbol/footprint library tables exist (a fresh KiCad
  install has none until the first-run dialog is answered, and every
  library-based tool depends on them).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..kicad import env

IS_WIN = sys.platform == "win32"
KICAD_PROCS = ["kicad", "eeschema", "pcbnew", "kicad-cli-gui", "pl_editor", "gerbview", "bitmap2component",
               "pcb_calculator"]


def _win_process_names() -> list[str] | None:
    """Image names of every process through a Toolhelp snapshot (about 50 ms; `tasklist` takes 3 s
    on a busy machine). None when the API is unavailable."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return None
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            names = []
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                names.append(e.szExeFile.lower())
                ok = k32.Process32NextW(snap, ctypes.byref(e))
            return names
        finally:
            k32.CloseHandle(snap)
    except Exception:  # noqa: BLE001
        return None


def running_kicad() -> list[str]:
    """Names of running KiCad GUI processes (kicad-cli is fine to have running)."""
    from . import demo
    if os.environ.get("COPPER_TEST_KICAD_CLOSED") == "1" and os.environ.get("COPPER_CONFIG_HOME_OVERRIDE"):
        return []        # tests in a sandbox: a KiCad open on the developer's machine is not the sandbox's KiCad
    if demo.active() is not None:
        # a KiCad started outside the demo uses the real config root, not the demo's: it cannot undo the demo
        return []
    found = set()
    try:
        names = _win_process_names() if IS_WIN else None
        if names is not None:
            for name in names:
                if name.endswith(".exe") and name[:-4] in KICAD_PROCS:
                    found.add(name[:-4])
        elif IS_WIN:
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15,
                                 creationflags=0x08000000).stdout
            for line in out.splitlines():
                name = line.split(",")[0].strip('"').lower()
                if name.endswith(".exe") and name[:-4] in KICAD_PROCS:
                    found.add(name[:-4])
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=15).stdout
            for line in out.splitlines():
                n = Path(line.strip()).name.lower()
                if n in KICAD_PROCS:
                    found.add(n)
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(found)


def pending_migration() -> Path | None:
    """The older KiCad config dir KiCad will offer to import on its first start.

    KiCad only offers "Import settings from a previous version" while its own
    ``<config>/<major.minor>`` folder does not exist. Creating that folder first
    (library tables, kicad_common.json) silently skips the import and the user
    loses their old libraries and preferences, so Copper waits for that start."""
    cd = env.config_dir()
    if cd.exists() or not re.fullmatch(r"\d+\.\d+", cd.name):
        return None
    key = lambda n: tuple(int(x) for x in n.split("."))  # noqa: E731
    try:
        olds = [d for d in cd.parent.iterdir() if d.is_dir() and re.fullmatch(r"\d+\.\d+", d.name)
                and key(d.name) < key(cd.name) and (d / "kicad_common.json").exists()]
    except OSError:
        return None
    return max(olds, key=lambda d: key(d.name)) if olds else None


def _migration_msg(prev: Path) -> str:
    return (f"KiCad {env.config_dir().name} has not been started yet; start it once and accept "
            f"'Import settings from KiCad {prev.name}' so your libraries and preferences carry over, "
            "then run `copper setup` again")


def _common_path() -> Path:
    return env.config_dir() / "kicad_common.json"


def api_status() -> dict:
    p = _common_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"path": str(p), "exists": False, "enabled": False}
    except (OSError, ValueError) as e:
        return {"path": str(p), "exists": True, "enabled": False, "error": str(e)}
    api = d.get("api") or {}
    return {"path": str(p), "exists": True, "enabled": bool(api.get("enable_server")),
            "interpreter_path": api.get("interpreter_path")}


def set_api(enabled: bool = True, force: bool = False) -> dict:
    """Flip api.enable_server. Refuses while KiCad is running unless force."""
    st = api_status()
    if st.get("enabled") == enabled and st.get("exists"):
        return {**st, "changed": False}
    prev = pending_migration()
    if prev is not None and not force:
        return {**st, "changed": False, "refused": _migration_msg(prev)}
    procs = running_kicad()
    if procs and not force:
        return {**st, "changed": False, "refused": f"KiCad is running ({', '.join(procs)}); close it first — "
                "KiCad rewrites kicad_common.json on exit and would undo the change."}
    p = Path(st["path"])
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError as e:
        return {**st, "changed": False, "refused": f"kicad_common.json is not valid JSON ({e}); open KiCad once to repair it"}
    api = d.setdefault("api", {})
    api["enable_server"] = bool(enabled)
    if enabled and not api.get("interpreter_path"):
        try:
            py = env.kicad_python()
            if py:
                pyw = py.with_name("pythonw.exe") if IS_WIN else py
                api["interpreter_path"] = str(pyw if pyw.exists() else py)
        except Exception:  # noqa: BLE001
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    bak = p.with_name(p.name + ".copper-backup")
    if p.exists() and not bak.exists():
        shutil.copy2(p, bak)
    tmp = p.with_name(p.name + ".copper-tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    after = api_status()
    return {**after, "changed": after.get("enabled") == enabled}


def lib_tables_status() -> dict:
    cd = env.config_dir()
    out = {"config_dir": str(cd)}
    for kind in ("sym", "fp"):
        p = cd / f"{kind}-lib-table"
        n = 0
        if p.exists():
            try:
                n = p.read_text(encoding="utf-8", errors="replace").count("(lib ")
            except OSError:
                pass
        out[kind] = {"path": str(p), "exists": p.exists(), "libs": n}
    return out


def ensure_lib_tables() -> dict:
    """Create missing global lib tables. Prefers kicad.libs.repair_tables
    (re-points stale entries too); falls back to copying KiCad's templates,
    which is exactly what KiCad's first-run dialog does."""
    before = lib_tables_status()
    missing = [k for k in ("sym", "fp") if not before[k]["exists"] or before[k]["libs"] == 0]
    result = {"before": before, "created": [], "method": None}
    prev = pending_migration() if missing else None
    if prev is not None:
        result.update(error=_migration_msg(prev), after=before)
        return result
    try:
        from ..kicad import libs  # built by the library module; optional here
        fn = getattr(libs, "repair_tables", None)
    except Exception:  # noqa: BLE001
        fn = None
    if fn is not None and missing:
        try:
            r = fn()
            result["method"] = "kicad.libs.repair_tables"
            result["repair"] = r if isinstance(r, (dict, list, str)) else str(r)
        except TypeError:
            try:
                r = fn(None)
                result["method"] = "kicad.libs.repair_tables"
                result["repair"] = r if isinstance(r, (dict, list, str)) else str(r)
            except Exception as e:  # noqa: BLE001
                result["repair_error"] = f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            result["repair_error"] = f"{type(e).__name__}: {e}"
    mid = lib_tables_status()
    still = [k for k in ("sym", "fp") if not mid[k]["exists"] or mid[k]["libs"] == 0]
    if still:
        try:
            inst = env.kicad()
        except Exception as e:  # noqa: BLE001
            result["error"] = f"KiCad not found: {e}"
            result["after"] = mid
            return result
        for k in still:
            src = inst.share / "template" / f"{k}-lib-table" if inst.share else None
            dst = Path(mid[k]["path"])
            if src and src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    shutil.copy2(dst, str(dst) + ".copper-backup")
                shutil.copy2(src, dst)
                result["method"] = result["method"] or "template-copy"
            else:
                result.setdefault("error", f"no template {k}-lib-table in {inst.share}")
    after = lib_tables_status()
    result["created"] = [k for k in ("sym", "fp") if after[k]["exists"] and after[k]["libs"] and
                         (not before[k]["exists"] or before[k]["libs"] == 0)]
    result["after"] = after
    return result
