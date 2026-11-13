"""Coming from KiCad: find what the user already has and offer to bring it along.

    discover()                         KiCad installs, config versions (with what each holds), the target
    plan(source=None)                  one Item per thing that can be used/imported, with its default answer
    apply(choices, source=None)        do the chosen items; every changed file is backed up and journalled
    undo(run_id=None)                  put everything from the last (or a named) run back
    history()                          the journal
    design_defaults()                  rules / net classes the user chose to adopt for new Copper projects

Items
    sym_libs, fp_libs   symbol / footprint library tables, including the user's own libraries
    env_vars            user path variables the libraries rely on (kicad_common.json environment.vars)
    themes              colour themes (colors/*.json)
    hotkeys             custom hotkeys (user.hotkeys)
    plugins             scripting / IPC plugins in Documents/KiCad/<version>
    other_settings      editor preferences (only while the new KiCad has no settings yet)
    recent              recent projects → Copper's own recent list
    design_defaults     design rules + net classes of a recent project → defaults for new Copper projects
    api                 enable KiCad's IPC API

Guarantees
    * Nothing is done that was not chosen. ``defaults(plan, yes=True)`` (what ``copper setup --yes`` uses)
      selects only items that do not modify KiCad's own files.
    * A file that already exists is copied to ``$COPPER_HOME/kicad-import/backups/<run>/`` before it is
      changed; a file Copper creates is recorded as created. ``undo`` restores the former and deletes the
      latter, and refuses to touch a file that was changed again afterwards (unless ``force``).
    * The source (the older KiCad version's settings) is only ever read.
    * KiCad rewrites its settings on exit, so items that change KiCad files are refused while KiCad runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import sexpr
from ..kicad import env
from . import clients, kicad_setup, prefs

_VER = re.compile(r"\d+\.\d+")
_STOCK = re.compile(r"^\$\{KICAD\d*_(?:SYMBOL|FOOTPRINT|3DMODEL|TEMPLATE)_DIR\}")
_THIRD = re.compile(r"\$\{KICAD(\d+)_3RD_PARTY\}")
_CLI_DEFAULTS = {"kicad.json", "kicad_common.json"}
KICAD_ITEMS = ("sym_libs", "fp_libs", "env_vars", "themes", "hotkeys", "plugins", "other_settings", "api")
COPPER_ITEMS = ("recent", "design_defaults")
ORDER = ("other_settings", "env_vars", "sym_libs", "fp_libs", "themes", "hotkeys", "plugins", "recent",
         "design_defaults", "api")


def _vkey(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


# ── locations ─────────────────────────────────────────────────────────────
def config_root() -> Path:
    return env.config_root()


def documents_root() -> Path:
    """``<Documents>/KiCad``. ``KICAD_DOCUMENTS_HOME`` (KiCad's own override) wins; in the test sandbox the
    fake home is used so the real Documents folder is never written."""
    v = os.environ.get("KICAD_DOCUMENTS_HOME")
    if v:
        return Path(v)
    o = clients.override_home()
    if o:
        return o / "Documents" / "KiCad"
    return env.documents_dir() / "KiCad"


def work_dir() -> Path:
    return clients.copper_home() / "kicad-import"


def journal_path() -> Path:
    return work_dir() / "journal.json"


# ── reading a config version ──────────────────────────────────────────────
def _json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _table_ok(text: str, kind: str | None = None) -> bool:
    """True when ``text`` parses as a KiCad library table (``(sym_lib_table …)`` / ``(fp_lib_table …)``)."""
    try:
        tree = sexpr.loads(text)
    except Exception:  # noqa: BLE001 - any parser failure means "not a table"
        return False
    head = str(tree[0]) if isinstance(tree, list) and tree else ""
    return head == f"{kind}_lib_table" if kind else head.endswith("_lib_table")


def _unreadable(table: Path) -> bool:
    """The file is there and holds something, but it is not a library table Copper can read."""
    try:
        text = table.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(text.strip()) and not _table_ok(text)


def _libs(table: Path) -> list[dict]:
    """Entries of a lib table: ``{'name','type','uri','options','descr','disabled','hidden','stock'}``."""
    try:
        tree = sexpr.loads(table.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - unreadable: :func:`_unreadable` tells the user
        return []
    out = []
    for e in sexpr.find_all(tree, "lib"):
        uri = str(sexpr.value(e, "uri", "") or "")
        out.append({"name": str(sexpr.value(e, "name", "") or ""), "type": str(sexpr.value(e, "type", "KiCad") or "KiCad"),
                    "uri": uri, "options": str(sexpr.value(e, "options", "") or ""),
                    "descr": str(sexpr.value(e, "descr", "") or ""),
                    "disabled": sexpr.find(e, "disabled") is not None, "hidden": sexpr.find(e, "hidden") is not None,
                    "stock": bool(_STOCK.match(uri))})
    return [x for x in out if x["name"]]


def _plugin_names(ver: str) -> dict[str, list[str]]:
    base = documents_root() / ver
    out = {}
    for rel in ("scripting/plugins", "plugins", "3rdparty/plugins"):
        d = base / rel
        try:
            names = sorted(x.name for x in d.iterdir() if not x.name.startswith((".", "__pycache__")))
        except OSError:
            names = []
        if names:
            out[rel] = names
    return out


def _recent(cfg: Path) -> list[str]:
    sysd = (_json(cfg / "kicad.json").get("system") or {})
    seen, out = set(), []
    for p in list(sysd.get("open_projects") or []) + list(sysd.get("file_history") or []):
        if isinstance(p, str) and p.lower().endswith(".kicad_pro") and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def scan(cfg: Path) -> dict:
    """What one KiCad config version holds (read-only)."""
    common = _json(cfg / "kicad_common.json")
    tables = {}
    for kind in ("sym", "fp"):
        libs = _libs(cfg / f"{kind}-lib-table")
        tables[kind] = {"exists": (cfg / f"{kind}-lib-table").exists(), "libs": len(libs),
                        "custom": [x["name"] for x in libs if not x["stock"]],
                        "unreadable": _unreadable(cfg / f"{kind}-lib-table")}
    try:
        themes = sorted(p.stem for p in (cfg / "colors").glob("*.json"))
    except OSError:
        themes = []
    try:
        files = sorted(p.name for p in cfg.iterdir() if p.is_file())
    except OSError:
        files = []
    recent = _recent(cfg)
    # `kicad-cli` (which Copper runs to find KiCad's version) creates <config>/<ver>/kicad.json and
    # kicad_common.json with defaults. Such a folder holds no settings of the user's yet: "fresh".
    fresh = not cfg.is_dir() or (set(files) <= _CLI_DEFAULTS and not themes)
    return {"version": cfg.name, "dir": str(cfg), "exists": cfg.is_dir(), "fresh": fresh,
            "sym": tables["sym"], "fp": tables["fp"],
            "themes": themes, "hotkeys": (cfg / "user.hotkeys").exists(),
            "env_vars": {k: v for k, v in (((common.get("environment") or {}).get("vars")) or {}).items() if v},
            "api": bool((common.get("api") or {}).get("enable_server")),
            "recent": recent, "recent_existing": [p for p in recent if os.path.exists(p)],
            "plugins": _plugin_names(cfg.name), "files": files}


def config_versions() -> list[dict]:
    root = config_root()
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and _VER.fullmatch(d.name)]
    except OSError:
        dirs = []
    out = [scan(d) for d in sorted(dirs, key=lambda d: _vkey(d.name), reverse=True)]
    return [s for s in out if s["files"] or s["themes"]]


def _real(cfgs: list[dict]) -> list[dict]:
    """Config versions that hold settings of the user's (not just kicad-cli's defaults)."""
    return [c for c in cfgs if not c["fresh"]]


def discover() -> dict:
    """Everything setup needs to ask its questions. Reads only."""
    installs = [i.to_dict() for i in env.installs()]
    try:
        active = env.kicad().to_dict()
    except Exception:  # noqa: BLE001
        active = None
    target = env.config_dir()
    cfgs = config_versions()
    return {"installs": installs, "active": active, "preferred_version": prefs.get("kicad_version"),
            "config_root": str(config_root()), "target": target.name, "target_dir": str(target),
            "target_exists": target.is_dir(), "target_fresh": scan(target)["fresh"], "configs": cfgs,
            "sources": [c["version"] for c in _real(cfgs)],
            "default_source": default_source(cfgs, target),
            "kicad_running": kicad_setup.running_kicad(), "documents": str(documents_root())}


def default_source(cfgs: list[dict], target: Path) -> str | None:
    """The target's own settings when it has any; otherwise the newest older version."""
    names = [c["version"] for c in _real(cfgs)]
    if target.name in names:
        return target.name
    older = [n for n in names if _vkey(n) < _vkey(target.name)]
    return max(older, key=_vkey) if older else (names[0] if names else None)


def choose_install(version: str | None) -> dict:
    """Remember which KiCad Copper should drive when several are installed (None = newest)."""
    if version:
        if not any((i.full_version or i.version).startswith(version) for i in env.installs()):
            raise ValueError(f"no installed KiCad matches '{version}'")
    prefs.set("kicad_version", version or None)
    # env.kicad() honours COPPER_KICAD_VERSION; an explicit variable from the user's shell is left alone
    if os.environ.get("COPPER_KICAD_VERSION_FROM_PREFS") or not os.environ.get("COPPER_KICAD_VERSION"):
        if version:
            os.environ["COPPER_KICAD_VERSION"] = version
            os.environ["COPPER_KICAD_VERSION_FROM_PREFS"] = "1"
        else:
            os.environ.pop("COPPER_KICAD_VERSION", None)
    env.refresh()
    return {"kicad_version": version, "active": (env.kicad().to_dict() if env.installs() else None)}


# ── plan ──────────────────────────────────────────────────────────────────
@dataclass
class Item:
    id: str
    label: str
    state: str                 # importable | in-use | nothing | blocked
    found: str = ""            # what was found, in words
    action: str = ""           # what choosing it does
    default: bool = False      # the answer offered when asked
    modifies_kicad: bool = False
    blocked: str = ""
    count: int = 0
    names: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _design_source(recent: list[str]) -> dict | None:
    for p in recent:
        d = _json(Path(p))
        classes = ((d.get("net_settings") or {}).get("classes")) or []
        rules = (((d.get("board") or {}).get("design_settings") or {}).get("rules")) or {}
        if classes or rules:
            return {"project": p, "rules": rules, "net_classes": classes}
    return None


def plan(source: str | None = None) -> dict:
    """What can be brought along from config version ``source`` (default: :func:`default_source`)."""
    target = env.config_dir()
    cfgs = config_versions()
    source = source or default_source(cfgs, target)
    src = next((c for c in cfgs if c["version"] == source), None)
    if source and src is None:
        raise ValueError(f"no KiCad settings for version '{source}' in {config_root()} "
                         f"(found: {', '.join(c['version'] for c in cfgs) or 'none'})")
    tgt = scan(target)
    same = src is not None and src["version"] == target.name
    running = kicad_setup.running_kicad()
    block = (f"KiCad is running ({', '.join(running)}); close it first, KiCad rewrites its settings on exit"
             if running else "")
    items: list[Item] = []

    def add(it: Item):
        if it.state == "importable" and it.modifies_kicad and block:
            it.state, it.blocked = "blocked", block
        items.append(it)

    if src is None:
        # no KiCad settings anywhere: only the pieces Copper can create make sense
        for kind, label in (("sym", "Symbol libraries"), ("fp", "Footprint libraries")):
            add(Item(f"{kind}_libs", label, "importable" if env.installs() else "nothing",
                     "KiCad has no global library table yet", "create it from KiCad's defaults", True, True))
        add(_api_item(tgt))
        return _plan_dict(None, target, tgt, items, cfgs)

    older = _vkey(src["version"]) < _vkey(target.name)
    frm = f"KiCad {src['version']}"

    # editor preferences: only meaningful before the new KiCad has written its own
    if not same and tgt["fresh"]:
        other = [f for f in src["files"] if f not in ("sym-lib-table", "fp-lib-table", "user.hotkeys")]
        add(Item("other_settings", "Editor preferences", "importable" if other else "nothing",
                 f"{len(other)} settings files in {frm}", f"copy them to KiCad {target.name} (what KiCad's own "
                 "'Import settings from a previous version' does)", True, True, count=len(other), names=other))

    # path variables
    new_vars = {k: v for k, v in src["env_vars"].items() if k not in tgt["env_vars"]}
    if src["env_vars"]:
        add(Item("env_vars", "Path variables", "in-use" if same else ("importable" if new_vars else "in-use"),
                 f"{_n(len(src['env_vars']), 'user path variable', 'user path variables')}: {', '.join(sorted(src['env_vars']))}",
                 "" if same or not new_vars else f"add {', '.join(sorted(new_vars))} to KiCad {target.name}",
                 True, True, count=len(new_vars), names=sorted(new_vars)))

    # library tables
    for kind, label in (("sym", "Symbol libraries"), ("fp", "Footprint libraries")):
        s, t = src[kind], tgt[kind]
        have = {x["name"] for x in _libs(target / f"{kind}-lib-table")}
        new = [n for n in s["custom"] if n not in have]
        found = _n(s["libs"], "library", "libraries") + (f", {len(s['custom'])} of your own ({_some(s['custom'])})" if s["custom"] else "")
        if s.get("unreadable") or t.get("unreadable"):
            bad = Path(src["dir"] if s.get("unreadable") else target) / f"{kind}-lib-table"
            add(Item(f"{kind}_libs", label, "nothing", f"{bad} is not a library table Copper can read (damaged?)",
                     "left alone; open KiCad's Manage Libraries dialog to repair it", False, True))
        elif same:
            if t["exists"] and t["libs"]:
                add(Item(f"{kind}_libs", label, "in-use", found, "Copper reads KiCad's library tables directly",
                         True, False, count=s["libs"], names=s["custom"]))
            else:
                add(Item(f"{kind}_libs", label, "importable", "KiCad has no global library table yet",
                         "create it from KiCad's defaults", True, True))
        elif not t["exists"] or not t["libs"]:
            add(Item(f"{kind}_libs", label, "importable", found + f" in {frm}",
                     f"create KiCad {target.name}'s table from its defaults" +
                     (f" and add {_n(len(s['custom']), 'library', 'libraries')} of your own" if s["custom"] else ""),
                     True, True, count=len(s["custom"]), names=s["custom"]))
        else:
            add(Item(f"{kind}_libs", label, "importable" if new else "in-use", found + f" in {frm}",
                     f"add {_n(len(new), 'library', 'libraries')} of your own to KiCad {target.name}'s table ({_some(new)})" if new
                     else f"KiCad {target.name} already lists all of them", True, True, count=len(new), names=new))

    # themes
    new = [t for t in src["themes"] if t not in tgt["themes"]]
    if src["themes"]:
        add(Item("themes", "Colour themes", "in-use" if same or not new else "importable",
                 f"{_n(len(src['themes']), 'theme', 'themes')}: {_some(src['themes'])}",
                 "" if same or not new else f"copy {_n(len(new), 'theme', 'themes')} to KiCad {target.name}", True, True,
                 count=len(new), names=new))

    # hotkeys
    if src["hotkeys"]:
        differs = not same and _sha(Path(src["dir"]) / "user.hotkeys") != _sha(target / "user.hotkeys")
        add(Item("hotkeys", "Hotkeys", "importable" if differs else "in-use", "custom hotkeys (user.hotkeys)",
                 f"copy them to KiCad {target.name}" + (" (replaces its current hotkeys; backed up)"
                                                         if tgt["hotkeys"] else "") if differs else "",
                 not tgt["hotkeys"], True, count=1 if differs else 0))

    # plugins
    sp, tp = src["plugins"], tgt["plugins"]
    names = [f"{rel}/{n}" for rel, ns in sp.items() for n in ns]
    new = [f"{rel}/{n}" for rel, ns in sp.items() for n in ns if n not in tp.get(rel, [])]
    if names:
        add(Item("plugins", "Plugins", "in-use" if same or not new else "importable",
                 f"{_n(len(names), 'plugin', 'plugins')}: {_some([n.rsplit('/', 1)[1] for n in names])}",
                 "" if same or not new else f"copy {len(new)} to Documents/KiCad/{target.name}", True, True,
                 count=len(new), names=new))

    # recent projects → Copper
    rec = src["recent_existing"]
    try:
        from .. import project as P
        known = set(P.recent())
    except Exception:  # noqa: BLE001
        known = set()
    new = [p for p in rec if p not in known]
    if src["recent"]:
        add(Item("recent", "Recent projects", "importable" if new else ("in-use" if rec else "nothing"),
                 f"{_n(len(src['recent']), 'recent project', 'recent projects')}, {len(rec)} still on disk",
                 f"add {len(new)} to Copper's recent projects ({_some([Path(p).stem for p in new])})" if new else
                 ("already in Copper's list" if rec else "none of them exists any more"), True, False,
                 count=len(new), names=new))

    # design rules / net classes
    ds = _design_source(rec)
    if ds:
        cur = design_defaults()
        same_ds = bool(cur and cur.get("project") == ds["project"] and cur.get("net_classes") == ds["net_classes"]
                       and cur.get("rules") == ds["rules"])
        ncls = [str(c.get("name")) for c in ds["net_classes"] if isinstance(c, dict)]
        add(Item("design_defaults", "Design rules and net classes", "in-use" if same_ds else "importable",
                 f"{Path(ds['project']).stem}: {_n(len(ncls), 'net class', 'net classes')} ({_some(ncls)})" +
                 (f", min clearance {ds['rules'].get('min_clearance')} mm" if "min_clearance" in ds["rules"] else ""),
                 "already Copper's defaults" if same_ds else
                 "use them as the defaults for new Copper projects",
                 False, False, count=len(ncls), names=[ds["project"]]))

    add(_api_item(tgt))
    return _plan_dict(src, target, tgt, items, cfgs, _left_behind(cfgs, target, tgt) if same else [])


def _left_behind(cfgs: list[dict], target: Path, tgt: dict) -> list[dict]:
    """Things an OLDER KiCad version's settings hold that the current version's do not. Shown when the
    plan is about the current version, so somebody who opened the new KiCad once (and so already has
    settings there) still learns that their own libraries from the old one can be brought along."""
    out = []
    for c in _real(cfgs):
        if _vkey(c["version"]) >= _vkey(target.name):
            continue
        bits = []
        for kind, one, many in (("sym", "symbol library", "symbol libraries"), ("fp", "footprint library", "footprint libraries")):
            have = {x["name"] for x in _libs(target / f"{kind}-lib-table")}
            new = [n for n in c[kind]["custom"] if n not in have]
            if new:
                bits.append(f"{_n(len(new), one, many)} of your own ({_some(new, 3)})")
        new = [t for t in c["themes"] if t not in tgt["themes"]]
        if new:
            bits.append(_n(len(new), "colour theme", "colour themes"))
        new = [n for rel, ns in c["plugins"].items() for n in ns if n not in tgt["plugins"].get(rel, [])]
        if new:
            bits.append(_n(len(new), "plugin", "plugins"))
        new = [k for k in c["env_vars"] if k not in tgt["env_vars"]]
        if new:
            bits.append(_n(len(new), "path variable", "path variables"))
        if bits:
            out.append({"version": c["version"], "summary": ", ".join(bits),
                        "how": f"copper setup --from {c['version']}"})
    return out


def _api_item(tgt: dict) -> Item:
    if tgt["api"]:
        return Item("api", "KiCad IPC API", "in-use", "enabled", "", True, True)
    return Item("api", "KiCad IPC API", "importable", "disabled",
                "enable it (lets Copper edit a board while it is open in KiCad)", True, True)


def _plan_dict(src, target, tgt, items, cfgs, also=None) -> dict:
    order = {k: i for i, k in enumerate(ORDER)}
    items.sort(key=lambda it: order.get(it.id, 99))
    return {"source": src["version"] if src else None, "source_dir": src["dir"] if src else None,
            "target": target.name, "target_dir": str(target), "target_exists": tgt["exists"],
            "target_fresh": tgt["fresh"],
            "same_version": bool(src and src["version"] == target.name),
            "sources": [c["version"] for c in _real(cfgs)], "items": [it.to_dict() for it in items],
            "also": list(also or []),
            "warnings": [f"KiCad {a['version']} also has {a['summary']} that KiCad {target.name} does not have. "
                         f"Choose the KiCad {a['version']} settings (`{a['how']}`) to bring them along."
                         for a in (also or [])] + ([f"KiCad {target.name} has no settings of its own yet. Once Copper imports anything, KiCad "
                          "will not show its own 'Import settings from a previous version' dialog on first start, so "
                          "choose 'Editor preferences' as well to bring everything across."]
                         if src and tgt["fresh"] and src["version"] != target.name else [])}


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _some(names: list[str], n: int = 4) -> str:
    names = [str(x) for x in names]
    return ", ".join(names[:n]) + (f" and {len(names) - n} more" if len(names) > n else "")


def defaults(pl: dict, yes: bool = False) -> dict[str, bool]:
    """The answer for each importable item. ``yes=True`` (non-interactive) never modifies KiCad's files."""
    out = {}
    for it in pl["items"]:
        if it["state"] != "importable":
            continue
        out[it["id"]] = bool(it["default"]) and not (yes and it["modifies_kicad"])
    return out


# ── journal ───────────────────────────────────────────────────────────────
def _sha(p: Path) -> str | None:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def history() -> list[dict]:
    runs = _json(journal_path()).get("runs")
    return [r for r in (runs if isinstance(runs, list) else []) if isinstance(r, dict) and r.get("id")]


def _journal_damaged() -> bool:
    p = journal_path()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(text.strip()) and not isinstance(_json(p).get("runs"), list)


def _replace(path: Path, data: bytes):
    """Write ``path`` atomically. When the file cannot be replaced (open in another program, read-only)
    nothing is left behind and the error says so."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".copper-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        if isinstance(e, PermissionError):
            raise PermissionError(f"{path} could not be written (read-only, or open in another program); "
                                  "it was left as it is") from None
        raise


def _save_history(runs: list[dict]):
    p = journal_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if _journal_damaged():
        # keep what is there for a human to read: it names the backups of earlier runs
        os.replace(p, p.with_name(f"journal.damaged-{time.strftime('%Y%m%d-%H%M%S')}.json"))
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "runs": runs}, indent=1), encoding="utf-8")
    os.replace(tmp, p)


class _Run:
    def __init__(self, source, target, dry_run=False):
        # Unique even for two runs in the same second: a run's backups live in a folder named after its id,
        # so a repeated id would overwrite the first run's copies of the user's original files.
        taken = {r.get("id") for r in history()}
        base = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 10000:04d}"
        self.id, n = base, 1
        while self.id in taken or (work_dir() / "backups" / self.id).exists():
            n += 1
            self.id = f"{base}-{n}"
        self.dry = dry_run
        self.ops: list[dict] = []
        self.rec = {"id": self.id, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source, "target": target,
                    "items": [], "ops": self.ops, "undone": False}
        self.backups = work_dir() / "backups" / self.id

    def write(self, path: Path, data: bytes, item: str) -> bool:
        """Create or replace ``path``; the previous content (if any) is saved first."""
        path = Path(path)
        old = None
        try:
            old = path.read_bytes()
        except OSError:
            pass
        if old == data:
            return False
        op = {"kind": "file", "item": item, "path": str(path), "backup": None, "created": old is None,
              "sha": hashlib.sha256(data).hexdigest()}
        if not self.dry:
            if old is not None:
                self.backups.mkdir(parents=True, exist_ok=True)
                b = self.backups / f"{len(self.ops):03d}-{path.name}"
                b.write_bytes(old)
                op["backup"] = str(b)
            if path.suffix == ".json":
                self.backups.mkdir(parents=True, exist_ok=True)
                w = self.backups / f"{len(self.ops):03d}-written-{path.name}"
                w.write_bytes(data)
                op["written"] = str(w)
            _replace(path, data)
        self.ops.append(op)
        return True

    def edit_json(self, path: Path, item: str, changes: list[tuple[list[str], object]]) -> list[list[str]]:
        """Set single keys of a JSON settings file and journal each one, so that undo can put exactly
        those keys back even after KiCad has re-saved the file (which it does on every exit, kicad-cli
        included). Returns the key paths that changed."""
        path = Path(path)
        exists = path.exists()
        d = _json(path) if exists else {}
        if exists and not d and path.read_text(encoding="utf-8", errors="replace").strip() not in ("", "{}"):
            raise ValueError(f"{path} is not valid JSON; open KiCad once to repair it")
        done = []
        for key, new in changes:
            node = d
            for k in key[:-1]:
                if not isinstance(node.get(k), dict):
                    node[k] = {}
                node = node[k]
            had = key[-1] in node
            old = node.get(key[-1])
            if had and old == new:
                continue
            node[key[-1]] = new
            done.append({"key": list(key), "had": had, "old": old, "new": new})
        if not done:
            return []
        op = {"kind": "json", "item": item, "path": str(path), "backup": None, "created": not exists, "changes": done}
        if not self.dry:
            if exists:
                self.backups.mkdir(parents=True, exist_ok=True)
                b = self.backups / f"{len(self.ops):03d}-{path.name}"
                shutil.copy2(path, b)
                op["backup"] = str(b)
            _replace(path, (json.dumps(d, indent=2) + "\n").encode("utf-8"))
        self.ops.append(op)
        return [c["key"] for c in done]

    def copy_new(self, src: Path, dst: Path, item: str) -> bool:
        """Copy a file or folder to a place where nothing exists yet."""
        if dst.exists():
            return False
        op = {"kind": "tree" if src.is_dir() else "file", "item": item, "path": str(dst), "backup": None,
              "created": True}
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
                op["files"] = {str(p.relative_to(dst)).replace("\\", "/"): _sha(p)
                               for p in sorted(dst.rglob("*")) if p.is_file()}
            else:
                shutil.copy2(src, dst)
                op["sha"] = _sha(dst)
                if dst.suffix == ".json":
                    # KiCad re-saves its JSON settings (adding default keys) on every run; keeping what was
                    # written lets undo tell "KiCad saved it again" from "the user changed it"
                    self.backups.mkdir(parents=True, exist_ok=True)
                    w = self.backups / f"{len(self.ops):03d}-written-{dst.name}"
                    shutil.copy2(dst, w)
                    op["written"] = str(w)
        self.ops.append(op)
        return True

    def commit(self):
        if self.dry or not self.ops:
            return
        runs = history()
        runs.append(self.rec)
        _save_history(runs)


# ── apply ─────────────────────────────────────────────────────────────────
def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lib_line(x: dict) -> str:
    return (f"\t(lib (name {_q(x['name'])}) (type {_q(x['type'])}) (uri {_q(x['uri'])}) (options {_q(x['options'])}) "
            f"(descr {_q(x['descr'])})" + (" (disabled)" if x["disabled"] else "") + (" (hidden)" if x["hidden"] else "") + ")")


def _rehome_uri(uri: str, src_ver: str, tgt_ver: str) -> str:
    """``${KICAD9_3RD_PARTY}/…`` keeps pointing at the KiCad 9 folder where the files really are."""
    def rep(m):
        if f"{m.group(1)}.0" == tgt_ver or m.group(1) == tgt_ver.split(".")[0]:
            return m.group(0)
        return str(documents_root() / src_ver / "3rdparty").replace("\\", "/")
    return _THIRD.sub(rep, uri)


def _template_table(kind: str) -> str:
    try:
        share = env.kicad().share
        t = share / "template" / f"{kind}-lib-table" if share else None
        if t and t.exists():
            return t.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    return f"({kind}_lib_table\n\t(version 7)\n)\n"


def _merge_table(run: _Run, kind: str, src_dir: Path | None, target: Path, same: bool) -> dict:
    dst = target / f"{kind}-lib-table"
    item = f"{kind}_libs"
    try:
        text = dst.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    created = False
    if text.strip() and not _table_ok(text):
        return {"error": f"{dst} is not a library table Copper can read; left alone "
                         "(open KiCad's Manage Libraries dialog to repair it)"}
    if not _libs_text(text):
        # no table yet, or one without a single library: start from KiCad's own default table
        text = _template_table(kind)
        created = True
    have = {x["name"] for x in _libs_text(text)}
    add = []
    if src_dir is not None and not same:
        for x in _libs(src_dir / f"{kind}-lib-table"):
            if x["stock"] or x["name"] in have:
                continue
            x = dict(x, uri=_rehome_uri(x["uri"], src_dir.name, target.name))
            add.append(x)
            have.add(x["name"])
    if add:
        body = text.rstrip()
        if not body.endswith(")"):
            return {"error": f"{dst} does not look like a KiCad library table; left alone"}
        nl = "\r\n" if "\r\n" in text else "\n"
        text = body[:-1].rstrip() + nl + nl.join(_lib_line(x) for x in add) + nl + ")" + nl
        sexpr.loads(text)        # must still parse
    changed = run.write(dst, text.encode("utf-8"), item) if (add or created) else False
    if changed and add and not created and not run.dry:
        # lets undo take exactly these entries out again after KiCad (or the user) has re-saved the table
        run.ops[-1]["added_libs"] = [{"name": x["name"], "uri": x["uri"]} for x in add]
    variables = env.path_vars()
    missing = [x["name"] for x in add if "${" in env.expand(x["uri"], variables)
               or (x["type"].lower() in ("kicad", "table") and not os.path.exists(env.expand(x["uri"], variables)))]
    out = {"changed": changed, "created": created and changed, "added": [x["name"] for x in add], "path": str(dst)}
    if missing:
        out["unresolved"] = missing
        out["note"] = (f"{_some(missing)}: the path does not exist on this computer, so "
                       f"{'it is' if len(missing) == 1 else 'they are'} listed but will not load until the path is fixed")
    return out


def _libs_text(text: str) -> list[dict]:
    try:
        tree = sexpr.loads(text)
    except Exception:  # noqa: BLE001
        return []
    return [{"name": str(sexpr.value(e, "name", "") or "")} for e in sexpr.find_all(tree, "lib")]


def _lib_spans(text: str) -> list[tuple[int, int, str, str]]:
    """``(start, end, name, uri)`` of every top-level ``(lib …)`` form of a table, by position in ``text``."""
    out, depth, i, n, start = [], 0, 0, len(text), None
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
            if depth == 2 and re.match(r"\(lib[\s(]", text[i:i + 5]):
                start = i
        elif c == ")":
            if depth == 2 and start is not None:
                try:
                    e = sexpr.loads(text[start:i + 1])
                    out.append((start, i + 1, str(sexpr.value(e, "name", "") or ""), str(sexpr.value(e, "uri", "") or "")))
                except Exception:  # noqa: BLE001
                    pass
                start = None
            depth -= 1
        i += 1
    return out


def _remove_libs(text: str, libs: list[dict]) -> tuple[str, int]:
    """``text`` without the entries Copper added (same name *and* same uri: an entry the user has
    re-pointed since is theirs now). Returns (new text, entries removed)."""
    want = {(x.get("name"), x.get("uri")) for x in libs}
    removed = 0
    for start, end, name, uri in reversed(_lib_spans(text)):
        if (name, uri) not in want:
            continue
        a = start
        while a > 0 and text[a - 1] in " \t":
            a -= 1
        b = end
        if text[b:b + 2] == "\r\n":
            b += 2
        elif text[b:b + 1] == "\n":
            b += 1
        text = text[:a] + text[b:]
        removed += 1
    return text, removed


def apply(choices: dict[str, bool], source: str | None = None, dry_run: bool = False, force: bool = False) -> dict:
    """Carry out the chosen items of :func:`plan`. Returns ``{'run','results','changed','undo'}``."""
    pl = plan(source)
    target = Path(pl["target_dir"])
    src_dir = Path(pl["source_dir"]) if pl["source_dir"] else None
    same = pl["same_version"]
    by_id = {it["id"]: it for it in pl["items"]}
    run = _Run(pl["source"], pl["target"], dry_run)
    results: dict[str, dict] = {}
    for iid in ORDER:
        if not choices.get(iid) or iid not in by_id:
            continue
        it = by_id[iid]
        if it["state"] == "blocked" and not force:
            results[iid] = {"skipped": it["blocked"]}
            continue
        if it["state"] in ("in-use", "nothing"):
            results[iid] = {"skipped": "nothing to do"}
            continue
        try:
            results[iid] = _do(run, iid, it, src_dir, target, same)
        except Exception as e:  # noqa: BLE001 - one failed item must not stop the others
            results[iid] = {"error": f"{type(e).__name__}: {e}"}
        run.rec["items"].append(iid)
    run.commit()
    env.refresh()
    changed = [op["path"] for op in run.ops]
    return {"run": run.id if run.ops else None, "dry_run": dry_run, "source": pl["source"], "target": pl["target"],
            "results": results, "changed": changed,
            "undo": "copper setup --undo" if run.ops and not dry_run else None}


def _do(run: _Run, iid: str, it: dict, src_dir: Path | None, target: Path, same: bool) -> dict:
    if iid in ("sym_libs", "fp_libs"):
        return _merge_table(run, iid[:-5], src_dir, target, same)
    if iid == "other_settings":
        n = 0
        for name in it["names"]:
            if name in _CLI_DEFAULTS and (target / name).exists():
                # only kicad-cli's defaults are there (the plan offered this item because the target is fresh)
                n += run.write(target / name, (src_dir / name).read_bytes(), iid)
            else:
                n += run.copy_new(src_dir / name, target / name, iid)
        for sub in ("toolbars", "3d"):
            if (src_dir / sub).is_dir():
                n += run.copy_new(src_dir / sub, target / sub, iid)
        return {"copied": n}
    if iid == "env_vars":
        src_vars = ((_json(src_dir / "kicad_common.json").get("environment") or {}).get("vars")) or {}
        have = ((_json(target / "kicad_common.json").get("environment") or {}).get("vars")) or {}
        keys = run.edit_json(target / "kicad_common.json", iid,
                             [(["environment", "vars", k], v) for k, v in src_vars.items() if v and k not in have])
        return {"added": [k[-1] for k in keys]}
    if iid == "themes":
        n = sum(run.copy_new(src_dir / "colors" / f"{t}.json", target / "colors" / f"{t}.json", iid) for t in it["names"])
        return {"copied": n}
    if iid == "hotkeys":
        ch = run.write(target / "user.hotkeys", (src_dir / "user.hotkeys").read_bytes(), iid)
        return {"changed": ch}
    if iid == "plugins":
        sbase, tbase = documents_root() / src_dir.name, documents_root() / target.name
        n = sum(run.copy_new(sbase / rel, tbase / rel, iid) for rel in it["names"])
        return {"copied": n, "into": str(tbase)}
    if iid == "recent":
        from .. import project as P
        st = P._state()
        before = list(st.get("recent") or [])
        added = [p for p in it["names"] if p not in before]
        if added and not run.dry:
            st["recent"] = (before + added)[:10]
            added = [p for p in added if p in st["recent"]]
            P._save_state(st)
        if added:
            run.ops.append({"kind": "recent", "item": iid, "path": str(P.STATE_FILE), "added": added})
        return {"added": added}
    if iid == "design_defaults":
        ds = _design_source(it["names"])
        if not ds:
            return {"skipped": "the project is gone"}
        data = {"project": ds["project"], "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "rules": ds["rules"], "net_classes": ds["net_classes"]}
        ch = run.write(work_dir() / "design_defaults.json", (json.dumps(data, indent=1) + "\n").encode("utf-8"), iid)
        return {"changed": ch, "project": ds["project"], "net_classes": len(ds["net_classes"])}
    if iid == "api":
        changes: list = [(["api", "enable_server"], True)]
        if not (_json(target / "kicad_common.json").get("api") or {}).get("interpreter_path"):
            try:
                py = env.kicad_python()
                if py:
                    pyw = py.with_name("pythonw.exe") if os.name == "nt" else py
                    changes.append((["api", "interpreter_path"], str(pyw if pyw.exists() else py)))
            except Exception:  # noqa: BLE001
                pass
        ch = bool(run.edit_json(target / "kicad_common.json", iid, changes))
        return {"changed": ch, "enabled": True, "note": "takes effect the next time KiCad starts"}
    return {"skipped": "unknown item"}


# ── undo ──────────────────────────────────────────────────────────────────
def undo(run_id: str | None = None, force: bool = False, dry_run: bool = False) -> dict:
    """Put back everything one run changed (default: the most recent run not yet undone)."""
    runs = history()
    cand = [r for r in runs if not r.get("undone") and (run_id is None or r["id"] == run_id)]
    if not cand:
        return {"run": None, "restored": [], "removed": [], "kept": [],
                "detail": "nothing to undo" if run_id is None else f"no run '{run_id}' to undo"}
    running = kicad_setup.running_kicad()
    r = cand[-1]
    touches_kicad = any(op.get("item") in KICAD_ITEMS for op in r["ops"])
    if running and touches_kicad and not force:
        return {"run": r["id"], "restored": [], "removed": [], "kept": [], "refused":
                f"KiCad is running ({', '.join(running)}); close it first, then run `copper setup --undo` again"}
    restored, removed, kept, notes = [], [], [], []
    virt: dict[str, str | None] = {}
    vjson: dict[str, dict] = {}        # what a JSON file holds after the key-level reverts so far (dry runs)
    def done(op):
        if not dry_run:
            op["undone"] = True

    for op in reversed(r["ops"]):
        if op.get("undone"):
            continue
        p = Path(op["path"])
        if op["kind"] == "recent":
            if not dry_run:
                from .. import project as P
                st = P._state()
                st["recent"] = [x for x in st.get("recent") or [] if x not in set(op.get("added") or [])]
                P._save_state(st)
            removed.append(f"{_n(len(op.get('added') or []), 'recent project', 'recent projects')} from Copper's list")
            done(op)
            continue
        if op["kind"] == "json":
            d = _json(p)
            if not p.exists() or virt.get(str(p), "") is None:
                done(op)
                continue
            if dry_run and str(p) in vjson:
                d = vjson[str(p)]              # a dry run writes nothing: carry the earlier reverts forward
            elif not d and _raw(p).strip() not in ("", "{}"):
                kept.append({"path": str(p), "why": "it is not valid JSON right now, so nothing in it was touched; "
                                                    f"the copy from before the import is at {op.get('backup') or 'n/a'}"})
                continue
            stale = []
            for c in reversed(op.get("changes") or []):
                node = d
                for k in c["key"][:-1]:
                    node = node.get(k) if isinstance(node, dict) else None
                leaf = c["key"][-1]
                if not isinstance(node, dict):
                    continue
                if leaf in node and node[leaf] != c["new"] and not force:
                    stale.append(".".join(c["key"]))      # the user (or KiCad) set it to something else since
                    continue
                if c["had"]:
                    node[leaf] = c["old"]
                else:
                    node.pop(leaf, None)
            if op.get("created") and not stale and not _leaves(d):
                # Copper created this file and nothing but its own keys was ever in it: take the file away
                # again (an empty settings folder left behind would make KiCad skip its first-start import)
                if not dry_run:
                    p.unlink()
                    _prune_empty(p.parent, 1)
                virt[str(p)] = None
                vjson.pop(str(p), None)
                if str(p) in restored:
                    restored.remove(str(p))
                removed.append(str(p))
                done(op)
                continue
            if not dry_run:
                _replace(p, (json.dumps(d, indent=2) + "\n").encode("utf-8"))
            virt.pop(str(p), None)
            vjson[str(p)] = d
            if str(p) not in restored:
                restored.append(str(p))
            if stale:
                kept.append({"path": str(p), "why": f"{', '.join(stale)} changed again since the import; left as it is now"})
            done(op)
            continue
        if op["kind"] == "tree":
            cur = {str(q.relative_to(p)).replace("\\", "/"): _sha(q) for q in sorted(p.rglob("*")) if q.is_file()} \
                if p.is_dir() else None
            if cur is None:
                done(op)
                continue
            if cur != (op.get("files") or {}) and not force:
                kept.append({"path": str(p), "why": "changed since the import"})
                continue
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
                _prune_empty(p.parent)
            removed.append(str(p))
            done(op)
            continue
        # one file can be changed by several items (kicad_common.json): in a dry run the later change
        # is not really reverted, so track what the file would hold by now
        cur = virt[str(p)] if str(p) in virt else _sha(p)
        if cur is None:
            if op.get("backup") and Path(op["backup"]).exists():
                if not dry_run:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(op["backup"], p)
                virt[str(p)] = _sha(Path(op["backup"]))
                if str(p) not in restored:
                    restored.append(str(p))
            done(op)
            continue
        if op.get("sha") and cur != op["sha"] and not force and op.get("added_libs") and str(p) not in virt:
            # KiCad or the user saved the table again: take out exactly the entries Copper added
            text = _raw(p)
            new, n = _remove_libs(text, op["added_libs"]) if _table_ok(text) else (text, 0)
            if n and _table_ok(new):
                if not dry_run:
                    _replace(p, new.encode("utf-8"))
                restored.append(str(p))
                left = len(op["added_libs"]) - n
                notes.append(f"{p.name}: removed the {n} librar{'y' if n == 1 else 'ies'} Copper added and kept "
                             "your later changes" + (f"; {left} had been changed since and stay" if left else ""))
                done(op)
                continue
        if op.get("sha") and cur != op["sha"] and not force and not _resaved(p, op, vjson.get(str(p))):
            kept.append({"path": str(p), "why": "changed since the import (KiCad or you edited it); "
                                               f"your original is at {op.get('backup') or 'n/a (Copper created the file)'}"})
            continue
        if op.get("created"):
            if not dry_run:
                p.unlink()
                _prune_empty(p.parent, 2)     # only folders that are empty again
            virt[str(p)] = None
            if str(p) in restored:
                restored.remove(str(p))
            removed.append(str(p))
            done(op)
        elif op.get("backup") and Path(op["backup"]).exists():
            if not dry_run:
                tmp = p.with_name(p.name + ".copper-tmp")
                shutil.copy2(op["backup"], tmp)
                os.replace(tmp, p)
            virt[str(p)] = _sha(Path(op["backup"]))
            if str(p) not in restored:
                restored.append(str(p))
            done(op)
        else:
            kept.append({"path": str(p), "why": "its backup is missing"})
    if not dry_run:
        r["undone"] = all(op.get("undone") for op in r["ops"])
        r["undone_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_history(runs)
        env.refresh()
    return {"run": r["id"], "dry_run": dry_run, "restored": restored, "removed": removed, "kept": kept,
            "notes": notes, "detail": f"run {r['id']} from {r['at']}: {len(restored)} restored, {len(removed)} removed"
                      + (f", {len(kept)} left alone" if kept else "")}


def _leaves(d) -> bool:
    """True when a JSON object holds any value at all (empty objects nested in empty objects do not count)."""
    if isinstance(d, dict):
        return any(_leaves(v) for v in d.values())
    return True


def _raw(p: Path) -> str:
    try:
        return Path(p).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _resaved(p: Path, op: dict, current: dict | None = None) -> bool:
    """True when a JSON settings file differs from what Copper wrote only because KiCad saved it again:
    every value Copper wrote is still there unchanged (KiCad adds its default keys on each save)."""
    w = op.get("written")
    if not w or not Path(w).exists():
        return False
    try:
        cur = current if current is not None else json.loads(p.read_text(encoding="utf-8"))
        was = json.loads(Path(w).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    def within(a, b):
        if isinstance(a, dict):
            return isinstance(b, dict) and all(k in b and within(v, b[k]) for k, v in a.items())
        return a == b
    return within(was, cur)


def _prune_empty(d: Path, levels: int = 3):
    """Remove folders an import created that are empty again (never above Documents/KiCad or the config root)."""
    stops = {str(documents_root()).lower(), str(config_root()).lower()}
    for _ in range(levels):
        if str(d).lower() in stops:
            return
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


# ── consumer side ─────────────────────────────────────────────────────────
def design_defaults() -> dict | None:
    """``{'project','rules','net_classes'}`` the user adopted for new projects, or None."""
    d = _json(work_dir() / "design_defaults.json")
    return d if d.get("net_classes") or d.get("rules") else None
