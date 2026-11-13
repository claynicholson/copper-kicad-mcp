"""Freerouting + Java for `copper freerouting`, the installer component and
doctor. A thin front end over :mod:`copper_kicad.pcb.freerouting`, which owns
the pinned versions, the download location (``$COPPER_HOME/tools``, default
``~/.copper/tools``) and the Java requirement, so the CLI and ``pcb_route``
always agree.

    find() -> {"ok", "available", "first_use_downloads", "java", "java_version", "jar", "version",
               "tools_dir", "problem", "fix"}
    install(progress=print) -> find()

``ok`` means Freerouting and Java are on disk now. ``available`` also covers the
normal state of a fresh install: nothing downloaded yet, and the first
``pcb_route`` fetches what ``first_use_downloads`` lists. That is reported as
working ("downloads on first use"), not as a missing feature.
"""
from __future__ import annotations

import shutil
from pathlib import Path

def _fix(tools_dir) -> str:
    where = tools_dir or "~/.copper/tools"
    return (f"run `copper freerouting --install` (downloads Freerouting + a Java runtime, about 120 MB, into {where}; "
            "COPPER_HOME moves it, so run it with the same COPPER_HOME your AI client uses)")


FIX = _fix(None)


def _backend():
    from ..pcb import freerouting as fr
    return fr


def find() -> dict:
    try:
        fr = _backend()
        s = fr.status()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "available": False, "first_use_downloads": [], "java": None, "java_version": None, "jar": None, "version": None,
                "tools_dir": str(default_tools_dir()),
                "problem": f"routing backend unavailable ({type(e).__name__}: {e})",
                "fix": "reinstall Copper: `uv tool install --reinstall copper-kicad`"}
    try:
        td = str(fr.tools_dir())
    except Exception:  # noqa: BLE001
        td = None
    ok = bool(s.get("ready"))
    first = list(s.get("first_route_downloads") or [])
    out = {"ok": ok, "available": ok or bool(first), "first_use_downloads": first,
           "java": s.get("java"), "java_version": s.get("java_version"),
           "jar": s.get("freerouting_jar"), "version": s.get("freerouting_version"), "tools_dir": td}
    if not out["jar"]:
        out["problem"] = f"Freerouting {out['version']} is not downloaded yet" if first else f"Freerouting {out['version']} is not installed"
    elif not out["java"]:
        out["problem"] = f"Java {getattr(fr, 'JAVA_MAJOR', '')}+ not found".replace("  ", " ")
    out["fix"] = None if ok else _fix(td)
    return out


def describe(f: dict) -> str:
    """One line for doctor / `copper freerouting` / copper_status."""
    where = f" in {f['tools_dir']}" if f.get("tools_dir") else ""
    if f.get("ok"):
        # say where the router really is: it can be found outside $COPPER_HOME/tools (a shared install)
        try:
            jar_dir = str(Path(f["jar"]).parent) if f.get("jar") else None
        except TypeError:
            jar_dir = None
        return f"freerouting {f.get('version') or '?'} + Java {f.get('java_version')}" + (f" in {jar_dir}" if jar_dir else where)
    if f.get("available"):
        return (f"downloads on first use: the first pcb_route fetches {' + '.join(f.get('first_use_downloads') or ['the router'])}"
                f"{' into ' + f['tools_dir'] if f.get('tools_dir') else ''} (needs network once)")
    return f"{f.get('problem') or 'not installed'}{where}"


def install(progress=print) -> dict:
    from . import demo
    demo.refuse_download()
    fr = _backend()
    say = progress or (lambda *_: None)
    last = {"msg": None}

    def prog(fraction, message=""):
        # pcb.freerouting reports (fraction, message) about once a second; print each new message
        msg = message or f"{int(fraction * 100)}%"
        if msg != last["msg"]:
            last["msg"] = msg
            say(msg)

    reused = reuse_shared(fr)
    if reused:
        say(f"Reused {', '.join(reused)} from {shared_tools_dir()} (no download)")
    say("Checking Java …")
    fr.ensure_java(prog)
    say("Checking Freerouting …")
    fr.ensure_jar(prog)
    res = find()
    say("Freerouting ready." if res["ok"] else f"Freerouting incomplete: {res.get('problem')}")
    return res


def default_tools_dir() -> Path:
    """pcb.freerouting.tools_dir() without importing it ($COPPER_HOME/tools)."""
    import os
    return Path(os.environ.get("COPPER_HOME") or Path.home() / ".copper") / "tools"


def shared_tools_dir() -> Path:
    """Where a plain install (no COPPER_HOME) keeps Freerouting + Java."""
    return Path.home() / ".copper" / "tools"


def reuse_shared(fr=None) -> list[str]:
    """With COPPER_HOME set, the router lives in ``$COPPER_HOME/tools``. Copy an
    existing ``~/.copper/tools`` install there instead of downloading ~120 MB again."""
    fr = fr or _backend()
    try:
        dst, src = Path(fr.tools_dir()), shared_tools_dir()
        if dst.resolve() == src.resolve() or not src.is_dir():
            return []
    except OSError:
        return []
    out = []
    jar = f"freerouting-{fr.FREEROUTING_VERSION}.jar"
    if (src / jar).is_file() and not (dst / jar).exists():
        shutil.copy2(src / jar, dst / jar)
        out.append(jar)
    jre = f"jre-{fr.JAVA_MAJOR}"
    if (src / jre).is_dir() and not (dst / jre).exists() and fr.find_java() is None:
        shutil.copytree(src / jre, dst / jre)
        out.append(jre)
    return out
