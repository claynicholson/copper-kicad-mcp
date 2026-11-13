"""Copper settings: one layered loader for everything that used to be an assumption.

Layers, later wins (dicts merge key by key, lists and scalars replace):

    built-in DEFAULTS
    < $COPPER_HOME/config.json              (this machine / this user)
    < <project>/.copper/config.json         (this design; travels with the project)
    < per-call arguments                    (parts_search(provider=…), fab_export(profile=…))

Keys::

    {
      "parts": {
        "providers": ["lcsc"],              # ordered; the first one that answers wins
        "provider_paths": [],               # extra provider modules: "my_provider.py" or "pkg.module"
        "allow_project_providers": false,   # USER file only: let a project's config load provider code
        "catalogue": null,                  # file(s) for the "local" provider (CSV or JSON)
        "options": {},                      # per-provider options: {"<provider>": {...}}
        "policy": {
          "require_in_stock": true,         # default of parts_search(in_stock=…) and of BOM sourcing
          "basic_only": false,              # only a provider's cheapest assembly class (JLC Basic)
          "allow_extended": true,           # allow a provider's most expensive assembly class
          "min_stock": 500,
          "part_field": null                # schematic field holding the supplier part number
        }                                   #   (null = the first provider's own field, e.g. "LCSC")
      },
      "fab": {"profile": "jlcpcb"}          # a built-in profile or one in $COPPER_HOME/fabs, <project>/.copper/fabs
    }

Provider modules are Python code, so `provider_paths` in a PROJECT file is ignored (with a warning in
copper_status) unless the USER file says "allow_project_providers": true: opening somebody else's project
must never run their code.

A broken file never crashes a tool: it raises ToolError(code='BAD_CONFIG') naming the file, the line and
the fix. Paths inside a config file are relative to the project folder (project file) or $COPPER_HOME
(user file). Standard library only; nothing here knows any vendor.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from .mcp.registry import ToolError

FILE_NAME = "config.json"

DEFAULTS: dict = {
    "parts": {
        "providers": ["lcsc"],
        "provider_paths": [],
        "allow_project_providers": False,
        "catalogue": None,
        "options": {},
        "policy": {
            "require_in_stock": True,
            "basic_only": False,
            "allow_extended": True,
            "min_stock": 500,
            "part_field": None,
        },
    },
    "fab": {"profile": "jlcpcb"},
}

# key path -> (type check, human description of what is accepted)
_SCHEMA = {
    "parts": (dict, "an object"),
    "parts.providers": ("names", 'a non-empty list of provider names, e.g. ["local", "kicad"]'),
    "parts.provider_paths": ("strings", 'a list of paths / module names, e.g. ["my_provider.py"]'),
    "parts.allow_project_providers": (bool, "true or false (only honoured in $COPPER_HOME/config.json)"),
    "parts.catalogue": ("paths", 'a file path or a list of file paths, e.g. "stockroom.csv"'),
    "parts.options": (dict, 'an object of per-provider options, e.g. {"local": {"in_stock_only": false}}'),
    "parts.policy": (dict, "an object"),
    "parts.policy.require_in_stock": (bool, "true or false"),
    "parts.policy.basic_only": (bool, "true or false"),
    "parts.policy.allow_extended": (bool, "true or false"),
    "parts.policy.min_stock": ("int", "a whole number ≥ 0"),
    "parts.policy.part_field": ("opt_str", 'a schematic field name such as "MPN", or null'),
    "fab": (dict, "an object"),
    "fab.profile": ("name", 'a fab profile name such as "jlcpcb", "pcbway", "oshpark" or "generic"'),
}
_PATH_KEYS = ("parts.catalogue", "parts.provider_paths")


def home() -> Path:
    return Path(os.environ.get("COPPER_HOME") or Path.home() / ".copper")


def user_file() -> Path:
    return home() / FILE_NAME


def project_file(pr) -> Path:
    return Path(pr.dir) / ".copper" / FILE_NAME


def _line_of(text: str, key: str) -> int | None:
    m = re.search(r'"' + re.escape(key.rsplit(".", 1)[-1]) + r'"\s*:', text)
    return text.count("\n", 0, m.start()) + 1 if m else None


def _bad(path: Path, message: str, fix: str, line: int | None = None, col: int | None = None) -> ToolError:
    where = str(path) + (f", line {line}" if line else "") + (f", column {col}" if col else "")
    return ToolError(f"bad Copper config {where}: {message}", code="BAD_CONFIG", fix=fix,
                     data={"file": str(path), **({"line": line} if line else {}), **({"column": col} if col else {})})


def read_file(path: Path) -> dict | None:
    """One layer, validated. None when the file does not exist."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise _bad(path, f"cannot be read ({e})", "make the file readable UTF-8 JSON, or delete it to use the defaults")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        hint = "fix the JSON syntax there"
        if "\\" in (text.splitlines()[e.lineno - 1] if e.lineno <= len(text.splitlines()) else ""):
            hint += ' (in JSON a Windows path needs doubled backslashes "C:\\\\parts\\\\stock.csv" or forward slashes)'
        raise _bad(path, e.msg, hint + ", or delete the file to use the defaults", e.lineno, e.colno)
    if not isinstance(data, dict):
        raise _bad(path, "the top level must be a JSON object", 'start the file with {"parts": {...}, "fab": {...}}', 1)
    _validate(data, path, text)
    return data


def _validate(data: dict, path: Path, text: str, prefix: str = "") -> None:
    for k, v in data.items():
        if k.startswith(("_", "$", "//")):
            continue                                   # comments / schema refs
        key = prefix + k
        if key not in _SCHEMA:
            if prefix == "parts.options.":
                continue
            valid = sorted(s[len(prefix):] for s in _SCHEMA if s.startswith(prefix) and "." not in s[len(prefix):])
            raise _bad(path, f"unknown key '{key}'", f"use one of {valid} (or remove it)", _line_of(text, key))
        kind, human = _SCHEMA[key]
        ok = True
        if kind is dict:
            ok = isinstance(v, dict)
        elif kind is bool:
            ok = isinstance(v, bool)
        elif kind == "int":
            ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
        elif kind == "opt_str":
            ok = v is None or (isinstance(v, str) and v.strip() != "")
        elif kind == "name":
            ok = isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][\w.\-]*", v or "") is not None
        elif kind == "names":
            ok = isinstance(v, list) and len(v) > 0 and all(isinstance(x, str) and x.strip() for x in v)
        elif kind == "strings":
            ok = isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
        elif kind == "paths":
            ok = v is None or (isinstance(v, str) and v.strip() != "") or \
                (isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v))
        if not ok:
            raise _bad(path, f"'{key}' must be {human} (got {json.dumps(v)[:60]})", f"set \"{k}\" to {human}",
                       _line_of(text, key))
        if kind is dict and key != "parts.options":
            _validate(v, path, text, key + ".")


def _absolutize(data: dict, base: Path) -> dict:
    """Paths in a config file are relative to that layer's base folder. Module names stay untouched."""
    parts = data.get("parts")
    if not isinstance(parts, dict):
        return data

    def fix(p: str, files_only: bool = False) -> str:
        if not files_only and not re.search(r"[\\/]|\.py$", p):
            return p                                    # 'pkg.module' — an importable name, not a file
        q = Path(os.path.expandvars(os.path.expanduser(p)))
        return str(q if q.is_absolute() else (base / q))

    if isinstance(parts.get("catalogue"), str):
        parts["catalogue"] = fix(parts["catalogue"], True)
    elif isinstance(parts.get("catalogue"), list):
        parts["catalogue"] = [fix(x, True) for x in parts["catalogue"]]
    if isinstance(parts.get("provider_paths"), list):
        parts["provider_paths"] = [fix(x) for x in parts["provider_paths"]]
    return data


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


class Config:
    """The merged settings plus where each layer came from."""

    def __init__(self, data: dict, sources: list[str], project_dir: Path | None = None, warnings: list | None = None):
        self.data = data
        self.sources = sources
        self.project_dir = project_dir
        self.warnings = warnings or []

    def get(self, dotted: str, default=None):
        cur = self.data
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def providers(self) -> list[str]:
        seen, out = set(), []
        for n in self.get("parts.providers") or []:
            n = str(n).strip().lower()
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out

    @property
    def policy(self) -> dict:
        return dict(self.get("parts.policy") or {})

    @property
    def fab_profile(self) -> str:
        return str(self.get("fab.profile") or "generic").strip().lower()

    def provider_options(self, name: str) -> dict:
        o = dict((self.get("parts.options") or {}).get(name) or {})
        if name == "local" and "catalogue" not in o and self.get("parts.catalogue"):
            o["catalogue"] = self.get("parts.catalogue")
        return o

    def describe(self) -> dict:
        d = {"parts_providers": self.providers, "fab_profile": self.fab_profile, "config_files": self.sources}
        if self.policy.get("part_field"):
            d["part_field"] = self.policy["part_field"]
        if self.warnings:
            d["warnings"] = self.warnings
        return d


def load(pr=None, overrides: dict | None = None) -> Config:
    """Merged settings for a project (or for no project: defaults + the user file)."""
    data = copy.deepcopy(DEFAULTS)
    sources = []
    layers = [(user_file(), home())]
    pdir = None
    if pr is not None:
        pdir = Path(pr.dir)
        layers.append((project_file(pr), pdir))
    warnings = []
    for i, (path, base) in enumerate(layers):
        layer = read_file(path)
        if layer is None:
            continue
        if i > 0 and isinstance(layer.get("parts"), dict):          # project layer
            lp = layer["parts"]
            lp.pop("allow_project_providers", None)
            if lp.get("provider_paths") and not data["parts"].get("allow_project_providers"):
                warnings.append(f"{path}: parts.provider_paths ignored — a project file may not load provider code. "
                                f"Move it to {user_file()}, or set \"allow_project_providers\": true there")
                lp.pop("provider_paths")
        _merge(data, _absolutize(layer, base))
        sources.append(str(path))
    if overrides:
        clean = _strip_none(overrides)
        if clean:
            _merge(data, clean)
            sources.append("call arguments")
    return Config(data, sources, pdir, warnings)


def _strip_none(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = _strip_none(v)
            if v:
                out[k] = v
        elif v not in (None, "", []):
            out[k] = v
    return out


def for_project(project=None, overrides: dict | None = None) -> Config:
    """Settings for a project path / the active project; falls back to the user layer when none is active."""
    from . import project as P
    pr = None
    try:
        pr = P.resolve(project, activate=False) if project else P.resolve(None)
    except ToolError:
        if project:
            raise
    return load(pr, overrides)


def set_project(pr, changes: dict) -> Path:
    """Merge `changes` into <project>/.copper/config.json (used by project_create(fab=…))."""
    path = project_file(pr)
    cur = read_file(path) or {}
    _merge(cur, changes)
    _validate(cur, path, json.dumps(cur))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def problems(cfg: Config, pr=None) -> list[dict]:
    """Settings that parse but cannot work: a provider name nothing provides, a provider module that does
    not load, a fab profile that does not exist or is broken, a catalogue file that is missing/unreadable.
    [{'message', 'fix'}]; empty = fine. Never raises."""
    out = []

    def add(e):
        out.append({"message": getattr(e, "message", str(e)), "fix": getattr(e, "fix", None)})

    try:
        from .parts import providers as PV
        for n in cfg.providers:
            try:
                bad = PV.get(n, cfg).health()
                if bad:
                    out.append({"message": str(bad.get("message")), "fix": bad.get("fix")})
            except ToolError as e:
                add(e)
            except Exception as e:  # noqa: BLE001 — third-party provider code
                out.append({"message": f"parts provider '{n}' failed: {type(e).__name__}: {e}",
                            "fix": "fix the provider module, or remove it from parts.providers"})
    except Exception as e:  # noqa: BLE001
        add(e)
    try:
        from .review import fab_profiles as FP
        FP.load(cfg.fab_profile, pr)
    except ToolError as e:
        add(e)
    except Exception as e:  # noqa: BLE001
        add(e)
    return out


def describe(pr=None) -> dict:
    """What copper_status / project_summary show. Never raises: a broken file is reported in place, and so
    is a file that parses but names a provider / fab profile / catalogue that does not exist (`problems`)."""
    try:
        cfg = load(pr)
    except ToolError as e:
        return {"error": e.code, "message": e.message, "fix": e.fix}
    d = cfg.describe()
    # an ignored project setting explains the 'unknown provider' that usually follows it: say it first
    bad = [{"message": w, "fix": None} for w in cfg.warnings] + problems(cfg, pr)
    if bad:
        d["problems"] = bad
    return d
