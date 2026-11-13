"""Model access drivers. ``base.py`` is the interface; every other module in this package is one driver.

    DRIVERS: {kind: Driver instance}
    register(cls) -> cls                 class decorator; a module registers its driver(s) at import
    get(kind) -> Driver | None
    describe() -> [{'kind','label','needs','available','reason'}]        what hello / accounts.list report
    load() -> None                       import every sibling module once; failures go to LOAD_ERRORS, not up

A driver module that fails to import (a missing optional program, a syntax error in someone's work in progress)
never takes the agent down: its kind is simply absent and ``hello.load_errors`` says why.
"""
from __future__ import annotations

import importlib
import pkgutil
import traceback

from .base import Cancelled, Driver, DriverError, TurnRequest, stream_lines  # noqa: F401

DRIVERS: dict = {}
LOAD_ERRORS: dict = {}
_loaded = False


def register(cls):
    inst = cls()
    if not inst.kind:
        raise ValueError(f"{cls.__name__} has no kind")
    DRIVERS[inst.kind] = inst
    return cls


def get(kind):
    load()
    return DRIVERS.get(kind)


def load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if m.name == "base":
            continue
        try:
            importlib.import_module(f"{__name__}.{m.name}")
        except Exception:  # noqa: BLE001
            LOAD_ERRORS[f"drivers.{m.name}"] = traceback.format_exc(limit=3).strip().splitlines()[-1]


def describe() -> list[dict]:
    load()
    out = []
    for kind, d in DRIVERS.items():
        try:
            ok, reason = d.available()
        except Exception as e:  # noqa: BLE001
            ok, reason = False, f"{type(e).__name__}: {e}"
        out.append({"kind": kind, "label": d.label or kind, "needs": list(d.needs), "available": bool(ok),
                    "reason": reason or ""})
    return out
