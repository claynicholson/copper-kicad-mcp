"""Parts providers: the plug-in point between Copper's tools and wherever part data comes from.

Built in:
  lcsc    LCSC / JLCPCB — offline Basic table, live JLC stock, EasyEDA symbol/footprint/3D import (default)
  local   your own catalogue file (CSV or JSON) — fully offline
  kicad   generic KiCad library parts, value + footprint only

Third-party providers are found two ways and need no change to Copper:
  * a Python entry point in group ``copper_kicad.parts`` (an installed package), or
  * ``parts.provider_paths`` in the Copper config: a ``.py`` file or an importable module name.

Which providers a call uses, in order: the tool's ``provider=`` argument, else ``parts.providers`` from
the layered config (config.py). See docs/PROVIDERS.md for the interface and a complete example.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import re
import sys
from pathlib import Path

from ...mcp.registry import ToolError
from .base import NEUTRAL_FIELDS, Provider, check

ENTRY_POINT_GROUP = "copper_kicad.parts"
MPN_FIELD_NAMES = ("MPN", "Manufacturer Part Number", "Manufacturer_Part_Number", "Mfr Part", "Mfr. Part #", "MFR_PN",
                   "Mfg Part #", "Part Number", "PartNumber", "Manufacturer Part", "Mfr_PN")

_PATH_CACHE: dict = {}
_EP_CACHE: dict | None = None


def _builtin() -> dict:
    from .kicad import KicadProvider
    from .lcsc import LcscProvider
    from .local import LocalProvider
    return {"lcsc": LcscProvider, "local": LocalProvider, "kicad": KicadProvider}


class _Duck(Provider):
    """Adapter for a provider object that does not subclass Provider: its own methods win."""

    def __init__(self, obj, options=None, project_dir=None):
        super().__init__(options, project_dir)
        self._obj = obj
        for attr in ("name", "label", "part_field", "assembly_classes", "generic", "needs_network"):
            if hasattr(obj, attr):
                setattr(self, attr, getattr(obj, attr))
        for m in ("search", "get", "owns", "pick", "resolve", "match_component", "alternatives", "status",
                  "schematic_fields", "source_bom", "health"):
            if callable(getattr(obj, m, None)):
                setattr(self, m, getattr(obj, m))


def _load_error(where: str, e: Exception | str, fix: str | None = None) -> ToolError:
    line = None
    if isinstance(e, SyntaxError):
        line = e.lineno
    elif isinstance(e, Exception) and e.__traceback__ is not None:
        tb = e.__traceback__
        while tb.tb_next is not None:
            tb = tb.tb_next
        line = tb.tb_lineno
    msg = f"{type(e).__name__}: {e}" if isinstance(e, Exception) else str(e)
    return ToolError(f"parts provider {where}" + (f", line {line}" if line else "") + f" could not be loaded: {msg}",
                     code="PROVIDER_LOAD_FAILED",
                     fix=fix or "fix the provider module (docs/PROVIDERS.md has a working example), or remove it from "
                                "parts.provider_paths / parts.providers in the Copper config",
                     data={"provider_source": where, **({"line": line} if line else {})})


def _things_in(mod) -> list:
    if hasattr(mod, "PROVIDERS"):
        return list(mod.PROVIDERS)
    if hasattr(mod, "PROVIDER"):
        return [mod.PROVIDER]
    for fn in ("providers", "provider"):
        if callable(getattr(mod, fn, None)):
            r = getattr(mod, fn)()
            return list(r) if isinstance(r, (list, tuple)) else [r]
    return [c for _n, c in inspect.getmembers(mod, inspect.isclass)
            if c.__module__ == mod.__name__ and not check(c)]


def _from_path(spec: str) -> dict:
    """{'name': factory} for one provider_paths entry (a .py file or a module name)."""
    is_file = bool(re.search(r"[\\/]|\.py$", spec))
    if is_file:
        p = Path(spec)
        try:
            st = p.stat()
        except OSError:
            raise _load_error(spec, "file not found", "fix the path in parts.provider_paths (relative paths are relative "
                                                      "to the config file's folder)")
        key = (str(p), st.st_mtime_ns)
        if key in _PATH_CACHE:
            return _PATH_CACHE[key]
        modname = "copper_provider_" + hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:12]
        try:
            sp = importlib.util.spec_from_file_location(modname, p)
            mod = importlib.util.module_from_spec(sp)
            sys.modules[modname] = mod
            sp.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001 — user code: report, never crash the server
            sys.modules.pop(modname, None)
            raise _load_error(spec, e)
    else:
        key = (spec, 0)
        if key in _PATH_CACHE:
            return _PATH_CACHE[key]
        try:
            mod = importlib.import_module(spec)
        except Exception as e:  # noqa: BLE001
            raise _load_error(spec, e, f"pip install the package that provides '{spec}' into the Python that runs Copper, "
                                       f"or give a .py file path instead")
    out = {}
    try:
        things = _things_in(mod)
    except Exception as e:  # noqa: BLE001
        raise _load_error(spec, e)
    for t in things:
        bad = check(t)
        if bad:
            raise _load_error(spec, f"{getattr(t, '__name__', t)!s} is not a provider: {'; '.join(bad)}")
        out[t.name] = t
    if not out:
        raise _load_error(spec, "the module defines no provider",
                          "define a class with `name`, `search` and `get` (subclass copper_kicad.parts.providers.Provider), "
                          "or a module-level PROVIDER = MyProvider")
    _PATH_CACHE[key] = out
    return out


def _entry_points() -> dict:
    global _EP_CACHE
    if _EP_CACHE is None:
        found = {}
        try:
            from importlib.metadata import entry_points
            for ep in entry_points(group=ENTRY_POINT_GROUP):
                found[ep.name.lower()] = ep
        except Exception:  # noqa: BLE001
            pass
        _EP_CACHE = found
    return _EP_CACHE


def available(cfg) -> dict:
    """{'name': source} for every provider this config can use (nothing is imported for entry points)."""
    out = {n: "built-in" for n in _builtin()}
    for n in _entry_points():
        out.setdefault(n, f"entry point {ENTRY_POINT_GROUP}")
    for spec in cfg.get("parts.provider_paths") or []:
        for n in _from_path(spec):
            out[n] = spec
    return out


def _instantiate(thing, cfg, name: str):
    opts = cfg.provider_options(name)
    pdir = cfg.project_dir
    if inspect.isclass(thing):
        try:
            obj = thing(opts, pdir)
        except TypeError:
            try:
                obj = thing()
            except Exception as e:  # noqa: BLE001
                raise _load_error(name, e)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _load_error(name, e)
    else:
        obj = thing
    if not isinstance(obj, Provider):
        obj = _Duck(obj, opts, pdir)
    return obj


def get(name: str, cfg) -> Provider:
    n = str(name or "").strip().lower()
    thing = None
    for spec in cfg.get("parts.provider_paths") or []:
        found = _from_path(spec)
        if n in found:
            thing = found[n]
    if thing is None and n in _builtin():
        thing = _builtin()[n]
    if thing is None and n in _entry_points():
        try:
            thing = _entry_points()[n].load()
        except Exception as e:  # noqa: BLE001
            raise _load_error(f"entry point '{n}'", e)
        bad = check(thing)
        if bad:
            raise _load_error(f"entry point '{n}'", "; ".join(bad))
    if thing is None:
        have = sorted(available(cfg))
        raise ToolError(f"unknown parts provider '{name}'", code="UNKNOWN_PROVIDER",
                        fix=f"use one of {have}; add your own with parts.provider_paths in the Copper config or an "
                            f"entry point in group '{ENTRY_POINT_GROUP}' (docs/PROVIDERS.md)", data={"providers": have})
    return _instantiate(thing, cfg, n)


def call(prov, method: str, *args, **kwargs):
    """Call one provider method. Third-party provider code can raise anything: turn that into a ToolError
    that names the provider (PROVIDER_FAILED) instead of an INTERNAL 'Copper bug'."""
    try:
        return getattr(prov, method)(*args, **kwargs)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001 — provider code is not ours
        name = getattr(prov, "name", "?")
        raise ToolError(f"parts provider '{name}' failed in {method}(): {type(e).__name__}: {e}", code="PROVIDER_FAILED",
                        fix=f"use another provider (provider=…) or fix '{name}' (its module, API key or network); "
                            f"copper_status lists the active providers")


def clean_results(prov, res) -> dict:
    """A provider's search() answer, made safe to use: a dict with a list of part-record dicts that each
    have an id. Anything else a third-party provider returns is dropped and counted in 'dropped'."""
    if not isinstance(res, dict):
        res = {"results": res if isinstance(res, list) else []}
    good, dropped = [], 0
    for r in res.get("results") or []:
        if isinstance(r, dict):
            try:
                prov.normalize(r)
            except Exception:  # noqa: BLE001
                r = {}
            if r.get("id"):
                good.append(r)
                continue
        dropped += 1
    res = dict(res, results=good)
    if dropped:
        res["dropped"] = f"{dropped} malformed record(s) from provider '{getattr(prov, 'name', '?')}' (no id / not an object)"
    return res


def active(cfg, provider: str | None = None) -> list[Provider]:
    """The providers a call should ask, in order."""
    names = [provider] if provider else cfg.providers
    return [get(n, cfg) for n in names]


def for_project(project=None, provider: str | None = None):
    """(config, providers) for a tool call. `project` is a path / None (active project, else user config)."""
    from ... import config
    cfg = config.for_project(project)
    return cfg, active(cfg, provider)


def for_pr(pr, provider: str | None = None):
    from ... import config
    cfg = config.load(pr)
    return cfg, active(cfg, provider)


def owner_of(part_id: str, cfg, provs: list[Provider]) -> Provider | None:
    """The provider a bare part id belongs to: 'name:id' prefix, else the first active one that owns it."""
    for p in provs:
        try:
            if p.owns(part_id):
                return p
        except ToolError:
            raise
        except Exception:  # noqa: BLE001
            continue
    return None


def split_id(part_id: str, cfg) -> tuple[str | None, str]:
    """'local:RC0603' → ('local', 'RC0603') when the prefix names a provider; else (None, id)."""
    s = str(part_id or "").strip()
    m = re.match(r"^([a-z0-9][a-z0-9_\-]*):(?!//)(.+)$", s)
    if m and m.group(1) in available(cfg):
        return m.group(1), m.group(2).strip()
    return None, s


def part_field(cfg, provs: list[Provider] | None = None) -> str | None:
    """The schematic field that holds 'the' supplier part number for this project: the policy's choice,
    else the first active provider that has one. None when only generic providers are active."""
    pf = cfg.policy.get("part_field")
    if pf:
        return str(pf)
    for p in provs if provs is not None else active(cfg):
        if p.part_field:
            return p.part_field
    return None


def generic_ok(provs: list[Provider]) -> bool:
    """True when a part needs no supplier number to count as sourced (a generic provider is active)."""
    return any(getattr(p, "generic", False) for p in provs)


def part_number(fields: dict, field: str | None) -> str:
    """The value of the part-number field on a component ('' when absent). Knows the usual spellings of
    the two fields that have many (LCSC, MPN); any other field is matched by name, case-insensitively."""
    if not field:
        return ""
    low = field.strip().lower()
    if low in ("lcsc", "lcsc part #", "lcsc part", "jlcpcb part #"):
        from ...review.common import lcsc_of
        return lcsc_of(fields)
    names = [n.lower() for n in (MPN_FIELD_NAMES if low == "mpn" else (field,))]
    for k, v in (fields or {}).items():
        if k.strip().lower() in names and str(v or "").strip() not in ("", "~", "-", "N/A", "n/a"):
            return str(v).strip()
    return ""


def resolve_part(pr, part_id: str, provider: str | None = None, **opts) -> dict:
    """A part id → what sch_apply needs to place it: {'lib_id','footprint','value','fields','provider',…}."""
    cfg, provs = for_pr(pr, None)
    pref, pid = split_id(part_id, cfg)
    name = provider or pref
    if name:
        prov = get(name, cfg)
    else:
        prov = owner_of(pid, cfg, provs)
        if prov is None and _builtin()["lcsc"](None, None).owns(pid):
            prov = get("lcsc", cfg)          # a C-number is unambiguous even when lcsc is not in the active list
        if prov is None:
            raise ToolError(f"no active parts provider knows part '{part_id}'", code="PART_NOT_FOUND",
                            fix=f"parts_search(query=...) to find it (active providers: {[p.name for p in provs]}), "
                                f"or name the provider: {{\"op\":\"place\",\"part\":\"{pid}\",\"provider\":\"<name>\"}}")
    r = call(prov, "resolve", pr, pid, **opts)
    if not isinstance(r, dict) or not r.get("lib_id"):
        raise ToolError(f"parts provider '{prov.name}' returned no symbol for part '{pid}'", code="NO_SYMBOL",
                        fix="place it with an explicit lib_id + footprint and set_field the part number")
    return _finish(prov, r)


def _finish(prov: Provider, r: dict) -> dict:
    r.setdefault("provider", prov.name)
    r["part_field"] = prov.part_field
    r.setdefault("fields", {})
    return r


def resolve_spec(pr, spec: str, provider: str | None = None, skip: tuple = ()) -> tuple[dict | None, list[str], list[str]]:
    """A free-text spec → (resolved part | None, warnings, provider names tried), asking the active
    providers in order. Providers named in `skip` are left to the caller."""
    cfg, provs = for_pr(pr, provider)
    warnings, tried = [], []
    for prov in provs:
        if prov.name in skip:
            continue
        tried.append(prov.name)
        try:
            rec = prov.pick(spec)
            if rec and not isinstance(rec, dict):
                raise TypeError(f"pick() returned {type(rec).__name__}, not a part record")
            r = prov.resolve(pr, rec) if rec else None
            if rec and (not isinstance(r, dict) or not r.get("lib_id")):
                raise TypeError("resolve() returned no lib_id")
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            warnings.append(f"provider '{prov.name}' failed on '{spec}': {type(e).__name__}: {e}")
            continue
        if not rec:
            continue
        r = _finish(prov, r)
        if rec.get("warning"):
            warnings.append(f"spec '{spec}': {rec['warning']}")
        for w in r.get("warnings") or []:
            warnings.append(f"spec '{spec}': {w}")
        if rec.get("value") and not r.get("value"):
            r["value"] = rec["value"]
        return r, warnings, tried
    return None, warnings, tried


def describe(pr=None) -> dict:
    """For copper_status / project_summary / doctor. Never raises."""
    from ... import config
    try:
        cfg = config.load(pr)
    except ToolError as e:
        return {"error": e.code, "message": e.message, "fix": e.fix}
    out = {"active": cfg.providers, "providers": []}
    for n in cfg.providers:
        try:
            out["providers"].append(get(n, cfg).status())
        except ToolError as e:
            out["providers"].append({"name": n, "error": e.message, "fix": e.fix})
    try:
        out["part_field"] = part_field(cfg)
        out["available"] = available(cfg)
    except ToolError as e:
        out["error"] = e.message
        out["fix"] = e.fix
    if cfg.warnings:
        out["warnings"] = cfg.warnings
    return out


__all__ = ["Provider", "NEUTRAL_FIELDS", "ENTRY_POINT_GROUP", "active", "available", "get", "for_project", "for_pr",
           "part_field", "part_number", "generic_ok", "resolve_part", "resolve_spec", "describe", "owner_of", "split_id",
           "call", "clean_results"]
