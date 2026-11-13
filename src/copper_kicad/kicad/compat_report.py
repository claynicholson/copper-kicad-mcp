"""The KiCad compatibility table behind ``copper doctor`` and ``copper status``.

``table()`` returns one row per installed KiCad (version, channel, whether it
is the active one and why, the file formats it reads, the PCB engine, what
its kicad-cli can and cannot do, known issues). ``checks()`` turns that into
doctor rows (plain dicts with the fields of ``install.doctor.Check``).

Only :func:`update_check` touches the network, and only when asked to.
"""
from __future__ import annotations

import os

from . import compat, env

# What the rows report about each kicad-cli: (label, command, flag). "+" present, "-" missing.
PROBES = [
    ("sch upgrade", "sch upgrade", None),
    ("refill-zones", "pcb drc", "--refill-zones"),
    ("check-zones", "pcb export gerbers", "--check-zones"),
    ("export stats", "pcb export stats", None),
    ("variants", "sch export netlist", "--variant"),
    ("jobsets", "jobset run", None),
    ("api-server", "api-server", None),
    ("diff", "pcb diff", None),
    ("png", "pcb export png", None),
]
# missing on the supported 10.0.x line by design (KiCad 11 features): not worth a "-" in the table
_FUTURE = {"api-server", "diff", "png"}


def _formats(inst) -> dict:
    out = {}
    for kind in ("sch", "pcb", "sym"):
        try:
            v = compat.max_stamp(inst, kind)
        except Exception:  # noqa: BLE001
            v = None
        if v:
            out[kind] = v
    return out


def _engine(inst) -> str:
    if compat.engine_available(inst):
        return "swig"
    v = inst.ver
    if v is not None and v.effective_major >= 11:
        return "none (pcbnew Python removed in KiCad 11)"
    return "none"


def row(inst, active, probe: bool = True) -> dict:
    v = inst.ver
    is_active = bool(active is not None and inst.cli and active.cli and str(inst.cli) == str(active.cli))
    d = {"version": inst.full_version or inst.version, "series": v.series if v else "",
         "channel": v.channel if v else "unknown", "root": str(inst.root), "active": is_active,
         "reason": (active.reason if is_active else _why_not(inst)), "formats": _formats(inst),
         "engine": _engine(inst), "advisories": compat.advisories(v)}
    caps: dict = {}
    if inst.cli and probe:
        try:
            c = compat.caps(inst)
            caps = {label: bool(c.has(cmd, flag)) for label, cmd, flag in PROBES}
        except Exception:  # noqa: BLE001
            caps = {}
    d["caps"] = caps
    return d


def _why_not(inst) -> str:
    v = inst.ver
    if v is None:
        return "not used (version unknown)"
    if (v.major, v.minor) < compat.FLOOR:
        return "not used (older than KiCad 10: view/verify only)"
    if v.prerelease:
        return "not used (pre-release; COPPER_KICAD_VERSION=nightly or a project in its format selects it)"
    return "not used (an install that suits the project better is active)"


def table(probe: bool = True) -> dict:
    """``{"installs": [row...], "active": version|None, "config_dir": ...}`` for ``copper doctor --json``.
    ``probe=False`` skips the kicad-cli capability columns (no subprocess: the fast path of copper_status)."""
    try:
        active = env.kicad()
    except Exception:  # noqa: BLE001
        active = None
    rows = [row(i, active, probe) for i in env.installs() if i.cli]
    return {"installs": rows, "active": (active.full_version if active else None),
            "config_dir": str(env.config_dir())}


def _caps_text(r: dict) -> str:
    caps = r.get("caps") or {}
    plus = [k for k in caps if caps[k] and k in _FUTURE]
    minus = [k for k in caps if not caps[k] and k not in _FUTURE]
    return " ".join(["+" + k.replace(" ", "-") for k in plus] + ["-" + k.replace(" ", "-") for k in minus])


def _row_status(r: dict) -> str:
    if not r["active"]:
        return "skip"
    if r["channel"] in ("nightly", "rc", "old", "unknown"):
        return "warn"
    if any(a["severity"] == "warn" for a in r["advisories"]):
        return "warn"
    return "ok"


def _row_text(r: dict) -> str:
    f = r["formats"]
    fmt = "  ".join(f"{k} {f[k]}" for k in ("sch", "pcb") if k in f)
    state = ("ACTIVE (" + r["reason"] + ")") if r["active"] else r["reason"]
    bits = [f"{r['version']}  {r['channel']}  {state}", fmt, "engine " + r["engine"], _caps_text(r)]
    return "  ".join(b for b in bits if b) + f"  at {r['root']}"


def checks(tbl: dict | None = None) -> list[dict]:
    """Doctor rows: ``kicad`` for the active install (+ its warn advisories),
    ``kicad_<version>`` for every other install, ``config_nested`` when the
    empty folder an old Copper left behind exists."""
    tbl = tbl or table()
    out = []
    for r in tbl["installs"]:
        st = _row_status(r)
        fix = None
        if r["active"]:
            warns = [a for a in r["advisories"] if a["severity"] == "warn"]
            if r["channel"] == "old":
                fix = f"Copper edits need KiCad 10; install it from {compat.DOWNLOAD_URL} (it can sit next to this one)"
            elif r["channel"] in ("nightly", "rc"):
                fix = (f"a pre-release is active; install a stable KiCad 10.0.x from {compat.DOWNLOAD_URL} "
                       "for everyday work (Copper then prefers it)")
            elif warns:
                fix = f"update KiCad at {compat.DOWNLOAD_URL}: {warns[0]['text']}"
            out.append({"id": "kicad", "label": "KiCad", "status": st, "detail": _row_text(r), "fix": fix,
                        "group": "core"})
        else:
            out.append({"id": "kicad_" + r["version"], "label": "KiCad (other install)", "status": st,
                        "detail": _row_text(r), "fix": None, "group": "core"})
    nested = nested_config_dir()
    if nested:
        n = sum(len(files) for _r, _d, files in os.walk(nested))
        if n:       # KiCad wrote settings there while an old Copper pointed it at this folder: not Copper's to delete
            out.append({"id": "config_nested", "label": "KiCad config folder", "status": "warn",
                        "detail": f"{nested} was created by Copper 1.0.0 or older and holds {n} file(s); "
                                  "KiCad itself does not use this folder",
                        "fix": "look at the files, then delete the folder by hand (copper doctor --fix leaves it alone)",
                        "group": "optional"})
        else:
            out.append({"id": "config_nested", "label": "KiCad config folder", "status": "warn",
                        "detail": f"{nested} is an empty folder left by Copper 1.0.0 or older; safe to delete",
                        "fix": "copper doctor --fix (removes it only if it is empty)", "group": "optional"})
    return out


def update_check(network: bool = True, timeout: float = 3.0) -> list[dict]:
    """``kicad_update`` row: warn when a newer supported KiCad exists; one info
    row when a newer *major* is out that Copper's PCB tools cannot use yet.
    Nothing at all when the answer is unknown (offline, opted out)."""
    try:
        active = env.kicad()
    except Exception:  # noqa: BLE001
        return []
    v = active.ver
    if v is None:
        return []
    try:
        d = compat.latest_release(timeout=timeout, cached_only=not network, detail=True)
    except Exception:  # noqa: BLE001
        return []
    out = []
    lat, newer = d.get("latest"), d.get("newer_major")
    if lat is not None and not v.prerelease and lat.t > v.t:
        why = [a["text"] for a in compat.advisories(v) if a["severity"] == "warn"]
        out.append({"id": "kicad_update", "label": "KiCad up to date", "status": "warn",
                    "detail": f"{v} installed, {lat} available" + (f" — {why[0]}" if why else ""),
                    "fix": compat.DOWNLOAD_URL, "group": "core"})
    elif lat is not None and not v.prerelease:
        out.append({"id": "kicad_update", "label": "KiCad up to date", "status": "ok",
                    "detail": f"{v} is the newest KiCad {lat.major} release", "fix": None, "group": "core"})
    if newer is not None and v.major < newer.major:
        out.append({"id": "kicad_next_major", "label": "KiCad " + str(newer.major), "status": "skip",
                    "detail": f"KiCad {newer} is out; Copper's PCB tools need KiCad {compat.SUPPORTED_MAX_MAJOR}.0.x "
                              "until a Copper release adds an engine for it (keep 10.0.x installed alongside)",
                    "fix": None, "group": "core"})
    return out


def nested_config_dir() -> str | None:
    """``<config>/<series>/<series>``: created by the Copper <= 1.0.0 worker bug
    (it exported KICAD_CONFIG_HOME=<config>/<series>, and KiCad appends the series itself)."""
    try:
        d = env.config_dir()
        n = d / d.name
        return str(n) if n.is_dir() else None
    except Exception:  # noqa: BLE001
        return None


def fix_nested_config_dir() -> dict:
    """Remove that folder — bottom-up with ``os.rmdir`` only, which cannot remove
    anything that still holds a file. CLI only (``copper doctor --fix``)."""
    n = nested_config_dir()
    if not n:
        return {"removed": False, "reason": "nothing to remove"}
    try:
        for root, dirs, files in os.walk(n, topdown=False):
            if files:
                return {"removed": False, "path": n, "reason": f"not empty ({os.path.join(root, files[0])}); left alone"}
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(n)
        return {"removed": True, "path": n}
    except OSError as e:
        return {"removed": False, "path": n, "reason": str(e)}
