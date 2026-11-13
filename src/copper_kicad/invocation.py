"""How the user started Copper, so every hint names a command that works for them.

Through the npm launcher (``npx -y copper-kicad ...``) there is no ``copper`` on PATH, so a hint such as
"run `copper doctor`" would fail. The launcher sets ``COPPER_NPM_LAUNCHER``; with it set, :func:`cmds`
rewrites ``copper <command>`` to ``npx -y copper-kicad <command>`` in text shown to the user.
"""
from __future__ import annotations

import os
import re

NPX = "npx -y copper-kicad"
_SUB = ("setup", "doctor", "install", "uninstall", "link", "status", "config", "freerouting", "render", "canvas",
        "kicad-api", "version", "serve", "--help", "--version")
# "copper" at a word start (or right after an ANSI colour code), never inside a path or a longer name
_CMD = re.compile(r"(^|[^\w./\\-]|\x1b\[[0-9;]*m)copper (?=(?:" + "|".join(re.escape(s) for s in _SUB)
                  + r")(?![\w-]))", re.M)


def via_npx() -> bool:
    return bool(os.environ.get("COPPER_NPM_LAUNCHER"))


def prog() -> str:
    """The command a user types to run Copper: ``copper`` or ``npx -y copper-kicad``."""
    return NPX if via_npx() else "copper"


def cmds(text):
    """``text`` with every ``copper <command>`` written the way this user can run it."""
    if not via_npx() or not isinstance(text, str) or "copper " not in text:
        return text
    return _CMD.sub(lambda m: m.group(1) + NPX + " ", text)


def cmds_obj(obj):
    """:func:`cmds` applied to every string in a JSON-like structure."""
    if not via_npx():
        return obj
    if isinstance(obj, str):
        return cmds(obj)
    if isinstance(obj, dict):
        return {k: cmds_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [cmds_obj(v) for v in obj]
    return obj


class CommandStream:
    """Wraps a text stream; everything written passes through :func:`cmds`."""

    def __init__(self, stream):
        self._s = stream

    def write(self, s):
        return self._s.write(cmds(s))

    def writelines(self, lines):
        for ln in lines:
            self.write(ln)

    def __getattr__(self, name):
        return getattr(self._s, name)
