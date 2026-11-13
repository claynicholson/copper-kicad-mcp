"""Copper's own small preference file, ``$COPPER_HOME/prefs.json``.

Holds choices made during setup (which KiCad to drive, the optional GitHub OAuth client id). Never a secret.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def path() -> Path:
    from . import clients
    return clients.copper_home() / "prefs.json"


def load() -> dict:
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def get(key: str, default=None):
    return load().get(key, default)


def export_env() -> None:
    """Make the choices that other modules read from the environment take effect in this process:
    the KiCad the user picked in setup becomes ``COPPER_KICAD_VERSION`` unless that is already set."""
    try:
        v = get("kicad_version")
        if v and not os.environ.get("COPPER_KICAD_VERSION"):
            os.environ["COPPER_KICAD_VERSION"] = str(v)
            os.environ["COPPER_KICAD_VERSION_FROM_PREFS"] = "1"
    except Exception:  # noqa: BLE001 - a damaged prefs file must never stop Copper from starting
        pass


def set(key: str, value) -> dict:  # noqa: A001 - mirrors dict API
    d = load()
    if value is None:
        d.pop(key, None)
    else:
        d[key] = value
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return d
