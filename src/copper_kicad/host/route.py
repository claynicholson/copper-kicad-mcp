"""Route an engine tool call to the live host of its project, when there is one.

This is the tool-call boundary for engines that run OUTSIDE copper-agent (the MCP server an agent CLI or any
other MCP client starts). Inside copper-agent the same job is done by ``LiveSession`` directly.

    registered_for(path) -> dict | None      is a live host registered for the project that holds this file?
    note_shadow(dir) / is_shadow(path)       shadow checkouts: never redirected, never snapshotted
    try_live(name, args, progress=None) -> wire result | None
                                             run the tool through the live host (shadow checkout -> engine ->
                                             uuid patch -> host.apply); None when no live host applies
    call_tool(name, args, progress=None) -> wire result
                                             drop-in for "prepare_args -> registry.call_tool -> to_wire":
                                             try_live first, else the plain file pipeline; a LIVE_HOST refusal
                                             of the file pipeline (the app opened the project in between) is
                                             retried through the host once

How a write is kept away from an open document
----------------------------------------------
``project.guard_writable`` (every writer calls it) asks ``registered_for``. When the project has a live host it
does not raise FILE_OPEN_IN_KICAD ("close KiCad"): it raises ``LIVE_HOST``, which means "this edit belongs to the
app: run the tool on a shadow checkout instead". ``call_tool`` here does exactly that. A caller that does not
go through ``call_tool`` still cannot clobber the open document: it gets the LIVE_HOST refusal with a fix text.

A project has a live host when (host/rendezvous.py, host/client.connect):
  * this process was started by copper-agent with COPPER_HOST_PORT + COPPER_HOST_TOKEN (and COPPER_HOST_PROJECT
    is unset or names the project), or
  * ``$COPPER_HOME/hosts/<key>.json`` exists for the project and its pid is alive.
Shadow checkouts (``<project>/.copper/shadow/<rev>/``) and everything the engine does while a LiveSession runs a
tool are never redirected: that IS the redirected call.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import threading
from pathlib import Path

from . import rendezvous

LIVE_HOST = "LIVE_HOST"
_INSIDE = contextvars.ContextVar("copper_live_inside", default=False)
_LOCK = threading.Lock()
_SESSIONS: dict = {}            # rendezvous key -> (LiveSession, HostClient, signature of the registration)


@contextlib.contextmanager
def inside():
    """Marks the engine work a LiveSession does for a host: guard_writable must not redirect it again."""
    token = _INSIDE.set(True)
    try:
        yield
    finally:
        _INSIDE.reset(token)


def project_dir_of(path) -> Path | None:
    """The project folder a design file belongs to: the nearest folder (up to 4 levels up) with a .kicad_pro."""
    p = Path(path)
    d = p if p.is_dir() else p.parent
    for _ in range(5):
        try:
            if any(d.glob("*.kicad_pro")):
                return d
        except OSError:
            return None
        if d.parent == d:
            break
        d = d.parent
    return None


_SHADOWS: set = set()           # shadow folders live hosts handed out in this process (they may be anywhere)


def _norm(p) -> str:
    # realpath, not abspath: one side may spell a folder 8.3 (USERNA~1) or through a link and the other in full
    try:
        return os.path.normcase(os.path.realpath(str(p)))
    except OSError:
        return os.path.normcase(os.path.abspath(str(p)))


def note_shadow(directory) -> None:
    """A host answered host.checkout with this folder: it is a shadow whatever its path looks like (an app may
    keep shadows outside the project, e.g. in a short temp location to stay under path-length limits)."""
    _SHADOWS.add(_norm(directory))
    while len(_SHADOWS) > 256:
        _SHADOWS.pop()


def is_shadow(path) -> bool:
    """Is this file or folder inside a live host's shadow checkout?"""
    try:
        return _in_shadow(Path(path))
    except (OSError, ValueError):
        return False


def _in_shadow(p: Path) -> bool:
    parts = [x.lower() for x in p.parts]
    if any(a == ".copper" and b == "shadow" for a, b in zip(parts, parts[1:])):
        return True
    if _SHADOWS:
        s = _norm(p)
        return any(s == d or s.startswith(d + os.sep) for d in _SHADOWS)
    return False


def _same_dir(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


def registered_for(path) -> dict | None:
    """The live host registered for the project that holds ``path`` (a design file or the project folder):
    {'project_dir', 'pid'?, 'via': 'env' | 'rendezvous'}, or None. Cheap: one glob and one small file read."""
    if _INSIDE.get():
        return None
    try:
        p = Path(path)
        if _in_shadow(p):
            return None
        d = project_dir_of(p)
        if d is None:
            return None
        port, token = os.environ.get("COPPER_HOST_PORT"), os.environ.get("COPPER_HOST_TOKEN")
        if port and port.isdigit() and token:
            mine = os.environ.get("COPPER_HOST_PROJECT")
            if not mine or _same_dir(mine, d):
                return {"project_dir": str(d), "via": "env"}
            return None
        info = rendezvous.lookup(d)
        if info:
            return {"project_dir": str(d), "pid": info.get("pid"), "via": "rendezvous"}
    except OSError:
        pass
    return None


def session(project_dir):
    """The LiveSession for a project's live host, connecting on first use; None when there is no host."""
    from .client import connect
    from .live import LiveSession
    if registered_for(project_dir) is None:
        return None
    d = project_dir_of(project_dir) or Path(project_dir)
    key = rendezvous.key(d)
    info = rendezvous.lookup(d) or {}
    sig = (info.get("port"), info.get("pid"), os.environ.get("COPPER_HOST_PORT"))
    with _LOCK:
        have = _SESSIONS.get(key)
        if have is not None and have[2] == sig:
            return have[0]
        if have is not None:
            _close(have[1])
        host = connect(d)
        if host is None:
            _SESSIONS.pop(key, None)
            return None
        sess = LiveSession(host, str(d))
        _SESSIONS[key] = (sess, host, sig)
        return sess


def forget(project_dir=None) -> None:
    """Drop the cached session(s): the next call connects again."""
    with _LOCK:
        keys = list(_SESSIONS) if project_dir is None else [rendezvous.key(project_dir_of(project_dir) or project_dir)]
        for k in keys:
            have = _SESSIONS.pop(k, None)
            if have is not None:
                _close(have[1])


def _close(host) -> None:
    try:
        close = getattr(getattr(host, "transport", None), "close", None)
        if close:
            close()
    except Exception:  # noqa: BLE001
        pass


def _project_dir(args: dict):
    from .. import project as P
    try:
        return P.resolve((args or {}).get("project"), must_exist=False, activate=False, format_check=False).dir
    except Exception:  # noqa: BLE001  (no active project, bad path: the plain pipeline reports it)
        return None


def _wire(outcome: dict) -> dict:
    content = [{"type": "text", "text": outcome.get("text") or ""}]
    content += [{"type": "image", "data": im.get("data"), "mimeType": im.get("mime", "image/png")}
                for im in outcome.get("images") or []]
    return {"content": content, "isError": not outcome.get("ok")}


def try_live(name: str, args: dict | None, progress=None) -> dict | None:
    """Run an engine tool through the live host of its project. -> the wire result ({'content', 'isError'}), or
    None when the tool does not write a live document or the project has no live host (the caller then runs the
    plain file pipeline). Never raises for host trouble: a host that went away gives None."""
    from ..mcp import registry
    from .. import tools as _tools  # noqa: F401  (registers the engine tools)
    from .live import route_of
    tool = registry.TOOLS.get(name) if isinstance(name, str) else None
    if route_of(tool) not in ("schematic", "board"):
        return None
    args = dict(args) if isinstance(args, dict) else {}
    d = _project_dir(args)
    if d is None:
        return None
    sess = session(d)
    if sess is None:
        return None
    out = sess.call_tool(name, args, progress)
    if (out.get("error") or {}).get("code") in ("NO_HOST", "HOST_GONE"):
        forget(d)
        return None
    return _wire(out)


def call_tool(name: str, args: dict | None, progress=None) -> dict:
    """An engine tool call for an MCP-style caller, landing in the live document when an app has it open."""
    from ..mcp import compact, registry
    res = try_live(name, args, progress)
    if res is not None:
        return res
    tool = registry.TOOLS.get(name) if isinstance(name, str) else None
    run_args, defaulted = compact.prepare_args(tool, args)
    raw = registry.call_tool(name, run_args, progress)
    data = raw.get("structuredContent") if isinstance(raw, dict) else None
    if isinstance(data, dict) and data.get("error") == LIVE_HOST:
        forget()
        res = try_live(name, args, progress)                 # the app opened the project since the first look
        if res is not None:
            return res
    d = _project_dir(args if isinstance(args, dict) else {})
    return compact.to_wire(raw, str(d) if d else None, tool=name, args=args if isinstance(args, dict) else {},
                           defaulted=defaulted)
