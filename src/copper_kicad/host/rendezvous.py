"""Where an engine process finds the live host of a project.

    $COPPER_HOME/hosts/<key>.json    {'port', 'token', 'pid', 'protocol', 'project_dir', 'name', 'started', 'methods'}

``key`` is the same project key as the focus store (focus/store.py): sha1(lower(resolved dir) | name)[:16].
The file is written by copper-agent when it starts its loopback relay (host/relay.py) and removed when the
agent exits. An entry whose pid is no longer running is stale: lookup() removes it and returns None.

    path(project_dir, name=None) -> Path
    publish(project_dir, name=None, *, port, token, pid=None, protocol=1, methods=None) -> Path
    lookup(project_dir, name=None) -> dict | None
    clear(project_dir, name=None, pid=None) -> None

The token is a bearer secret for loopback only; the file is chmod 600 where the OS supports it and lives in
the user's own $COPPER_HOME, never in the project folder.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("COPPER_HOME") or (Path.home() / ".copper"))


def project_name(project_dir, name: str | None = None) -> str:
    """The project's name: the stem of its .kicad_pro (the one named like the folder, else the first)."""
    if name:
        return name
    d = Path(project_dir)
    try:
        pros = sorted(p.stem for p in d.glob("*.kicad_pro"))
    except OSError:
        pros = []
    return d.name if d.name in pros or not pros else pros[0]


def key(project_dir, name: str | None = None) -> str:
    d = Path(project_dir)
    try:
        d = d.resolve()
    except OSError:
        pass
    return hashlib.sha1(f"{str(d).lower()}|{project_name(project_dir, name)}".encode()).hexdigest()[:16]


def path(project_dir, name: str | None = None) -> Path:
    return _home() / "hosts" / f"{key(project_dir, name)}.json"


def publish(project_dir, name: str | None = None, *, port: int, token: str, pid: int | None = None,
            protocol: int = 1, methods=None) -> Path:
    p = path(project_dir, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    info = {"port": int(port), "token": token, "pid": int(pid or os.getpid()), "protocol": protocol,
            "project_dir": str(project_dir), "name": project_name(project_dir, name), "started": round(time.time(), 3)}
    if methods:
        info["methods"] = list(methods)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f, separators=(",", ":"))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return p


def lookup(project_dir, name: str | None = None) -> dict | None:
    p = path(project_dir, name)
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not isinstance(info.get("port"), int) or not isinstance(info.get("token"), str):
        return None
    if not pid_alive(info.get("pid")):
        try:
            p.unlink()
        except OSError:
            pass
        return None
    return info


def clear(project_dir, name: str | None = None, pid: int | None = None) -> None:
    """Remove the entry. With ``pid``, only if that process wrote it (never unpublish another agent's host)."""
    p = path(project_dir, name)
    if pid is not None:
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("pid") != pid:
                return
        except (OSError, ValueError, AttributeError):
            return
    try:
        p.unlink()
    except OSError:
        pass


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True
