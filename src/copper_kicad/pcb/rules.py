"""Fab design rules (from the fab profile; JLCPCB by default): .kicad_pro design settings + net
classes, and a .kicad_dru custom-rules block.

KiCad 9/10 keep board constraints and net classes in the project file, so we
edit that JSON directly (pcbnew workers save boards with settings skipped).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import project as projmod

# Design-rule presets. The fab-specific ones are DATA: every fab profile (review/fab_profiles.py,
# data/fabs/*.json, plus the user's own in $COPPER_HOME/fabs and <project>/.copper/fabs) contributes
# '<profile>' and '<profile>_advanced', and the default profile also answers to its short names 'jlc' /
# 'jlc_advanced'. 'fab' is the project's active profile. Only the fab-independent preset lives here.
_GENERIC_PRESETS = {
    "relaxed": {        # hand-solderable / any fab
        "label": "relaxed (0.2 mm / 0.8 mm vias)",
        "min_track": 0.2, "min_clearance": 0.2, "min_via_diameter": 0.8, "min_via_drill": 0.4,
        "min_annular": 0.2, "min_hole": 0.4, "hole_to_hole": 0.5, "edge_clearance": 0.5,
        "hole_clearance": 0.3, "silk_clearance": 0.0, "min_text_height": 1.0, "min_text_thickness": 0.15,
        "via": (0.8, 0.4),
    },
}


def preset_rules(name: str, pr=None) -> dict | None:
    """The rule set behind a pcb_setup(rules=…) name, or None when the name is unknown."""
    if name in _GENERIC_PRESETS:
        return _GENERIC_PRESETS[name]
    from ..review import fab_profiles as FP
    layers = None
    if pr is not None and pr.pcb.exists():
        try:
            from ..review.fab import copper_layers
            layers = len(copper_layers(pr.pcb))
        except Exception:  # noqa: BLE001
            layers = None
    return FP.preset(name, pr, layers)


def preset_names(pr=None) -> list[str]:
    from ..review import fab_profiles as FP
    return list(dict.fromkeys(FP.preset_names(pr) + list(_GENERIC_PRESETS)))


def default_preset(pr=None) -> str:
    """The rule preset a first-time board setup gets: the project's fab profile (its short alias when it
    has one, so the default stays 'jlc')."""
    from ..review import fab_profiles as FP
    prof = FP.active(pr)
    for alias, tier in (prof.get("presets") or {}).items():
        if tier == "standard":
            return alias
    return prof["name"]


def _require_preset(name: str, pr=None) -> dict:
    p = preset_rules(name, pr)
    if p is None:
        from ..mcp.registry import ToolError
        raise ToolError(f"unknown design-rule preset '{name}'", code="BAD_ARGUMENT",
                        fix=f"use one of {preset_names(pr)} ('fab' = the project's fab profile) or 'none'")
    return p


class _Presets(dict):
    """Mapping view kept for callers that index PRESETS[name]: generic presets + every fab profile's."""

    def __missing__(self, name):
        p = preset_rules(name)
        if p is None:
            raise KeyError(name)
        return p

    def get(self, name, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def __contains__(self, name):
        return self.get(name) is not None


PRESETS = _Presets(_GENERIC_PRESETS)

# every entry is matched against KiCad's full net name ('/VBAT' for a local label): see power_variants()
POWER_PATTERNS = ["GND", "*GND", "GND*", "*/GND*", "+*", "*/+*", "*VCC*", "*VDD*", "*VBUS*", "*VIN*", "*VBAT*",
                  "*VSYS*", "*VMOT*", "*PWR*", "*3V3*", "*5V*", "*12V*", "*24V*", "*1V8*", "*VREG*", "*VOUT*"]


def _netclass(name, clearance, track, via_d, via_dr, priority):
    return {"bus_width": 12, "clearance": clearance, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
            "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
            "name": name, "pcb_color": "rgba(0, 0, 0, 0.000)", "priority": priority,
            "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": track, "tuning_profile": "",
            "via_diameter": via_d, "via_drill": via_dr, "wire_width": 6}


def load_pro(pr) -> dict:
    try:
        return json.loads(pr.pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"meta": {"filename": pr.pro.name, "version": 3}}


def power_variants(pat: str) -> list[str]:
    """KiCad matches net-class patterns against the FULL net name, and a local label's net is
    '/NAME' (or '/sheet/NAME'). A bare 'VBAT*' or 'BATN' therefore never matches '/VBAT': add a
    '*/'-prefixed twin for every pattern that does not already start with '*' or '/'."""
    pat = str(pat).strip()
    if not pat:
        return []
    if pat[0] in "*/" or pat.startswith("^"):
        return [pat]
    return [pat, "*/" + pat]


def expand_patterns(pats) -> list[str]:
    out = []
    for p in pats or []:
        for v in power_variants(p):
            if v not in out:
                out.append(v)
    return out


def pattern_matches(patterns, nets) -> dict:
    """{pattern: number of board nets it matches} (KiCad-style wildcard, full net name)."""
    from fnmatch import fnmatchcase
    nets = list(nets)
    return {p: sum(1 for n in nets if fnmatchcase(n, p)) for p in patterns}


def _class(ns: dict, name: str) -> dict | None:
    return next((c for c in ns.get("classes", []) if c.get("name") == name), None)


def normalize_project(pr, while_open: str = "refuse") -> list[str]:
    """Give every Power-class pattern in the .kicad_pro its '*/' twin (templates and older
    projects used 'VBAT*', which misses '/VBAT'). Idempotent; returns the patterns added."""
    if not pr.pro.exists():
        return []
    pro = load_pro(pr)
    ns = pro.get("net_settings") or {}
    pats = ns.get("netclass_patterns") or []
    have = {(x.get("netclass"), x.get("pattern")) for x in pats}
    added = []
    for x in list(pats):
        if x.get("netclass") != "Power":
            continue
        for v in power_variants(x.get("pattern", "")):
            if ("Power", v) not in have:
                have.add(("Power", v))
                pats.append({"netclass": "Power", "pattern": v})
                added.append(v)
    if added:
        projmod.guard_writable(pr.pro, while_open)
        ns["netclass_patterns"] = pats
        projmod.atomic_write(pr.pro, json.dumps(pro, indent=2) + "\n")
    return added


def set_netclasses(pr, default_track: float | None = None, power_track: float | None = None,
                   via: tuple | None = None, power_via: tuple | None = None, extra_power: list | None = None,
                   default_clearance: float | None = None, preset: str | None = None,
                   replace_power_patterns: list | None = None, pro: dict | None = None,
                   write: bool = True, while_open: str = "refuse") -> dict:
    """Edit only the Default / Power net classes and the Power patterns. Anything not passed keeps
    the value already in the project (a template's 0.15 mm Default or 0.5 mm Power survives)."""
    p = preset_rules(preset, pr) if preset else None
    pro = pro if pro is not None else load_pro(pr)
    ns = pro.setdefault("net_settings", {})
    ns.setdefault("meta", {"version": 5})
    d0, p0 = _class(ns, "Default") or {}, _class(ns, "Power") or {}
    min_track = p["min_track"] if p else 0.0
    dt = default_track if default_track is not None else d0.get("track_width", 0.2)
    dc = default_clearance if default_clearance is not None else d0.get("clearance", 0.2)
    if p:
        dt, dc = max(dt, p["min_track"]), max(dc, p["min_clearance"])
    pt = max(power_track if power_track is not None else p0.get("track_width", 0.4), min_track)
    if via:
        v = tuple(via)
    elif d0.get("via_diameter"):
        v = (d0["via_diameter"], d0.get("via_drill", 0.3))
    else:
        v = tuple(p["via"]) if p else (0.6, 0.3)
    if p:
        v = (max(v[0], p["min_via_diameter"]), max(v[1], p["min_via_drill"]))
    if power_via:
        pv = tuple(power_via)
    elif p0.get("via_diameter"):
        pv = (max(p0["via_diameter"], v[0]), max(p0.get("via_drill", 0.4), v[1]))
    else:
        pvd = tuple((p or {}).get("power_via") or (0.8, 0.4))     # the preset's own power via (tighter for 'advanced' tiers)
        pv = (max(v[0], pvd[0]), max(v[1], pvd[1]))
    pc = max(p0.get("clearance", dc), dc) if (default_clearance is None and p0) else dc
    classes = [c for c in ns.get("classes", []) if c.get("name") not in ("Default", "Power")]
    dcl = dict(d0)
    dcl.update(_netclass("Default", dc, dt, v[0], v[1], 2147483647))
    pcl = dict(p0)
    pcl.update(_netclass("Power", pc, pt, pv[0], pv[1], 0))
    classes.insert(0, dcl)
    classes.append(pcl)
    ns["classes"] = classes
    pats = list(ns.get("netclass_patterns") or [])
    if replace_power_patterns is not None:
        pats = [x for x in pats if x.get("netclass") != "Power"]
        base = list(replace_power_patterns)
    else:
        base = [x["pattern"] for x in pats if x.get("netclass") == "Power"]
    for pat in expand_patterns(base + list(extra_power or [])):
        if not any(x.get("netclass") == "Power" and x.get("pattern") == pat for x in pats):
            pats.append({"netclass": "Power", "pattern": pat})
    ns["netclass_patterns"] = pats
    if write:
        projmod.guard_writable(pr.pro, while_open)
        pro.setdefault("meta", {"filename": pr.pro.name, "version": 3})
        projmod.atomic_write(pr.pro, json.dumps(pro, indent=2) + "\n")
    return {"Default": {"track": dt, "clearance": dc, "via": list(v)},
            "Power": {"track": pt, "clearance": pc, "via": list(pv),
                      "patterns": [x["pattern"] for x in pats if x.get("netclass") == "Power"]}}


def apply(pr, preset: str = "jlc", default_track: float | None = None, default_clearance: float | None = None,
          power_track: float | None = None, power_via: tuple | None = None, via: tuple | None = None,
          power_patterns: list[str] | None = None, silk_text: float = 1.0, silk_thickness: float = 0.15,
          while_open: str = "refuse", extra_power: list[str] | None = None) -> dict:
    """Write rules + net classes into the .kicad_pro and the .kicad_dru. Net-class values that are
    not passed keep what the project already has (first-time defaults: Default 0.2, Power 0.4)."""
    p = _require_preset(preset, pr)
    pro = load_pro(pr)
    projmod.guard_writable(pr.pro, while_open)
    board = pro.setdefault("board", {})
    ds = board.setdefault("design_settings", {})
    rules = ds.setdefault("rules", {})
    rules.update({
        "min_clearance": p["min_clearance"], "min_track_width": p["min_track"],
        "min_via_diameter": p["min_via_diameter"], "min_through_hole_diameter": p["min_hole"],
        "min_via_annular_width": p["min_annular"], "min_hole_to_hole": p["hole_to_hole"],
        "min_copper_edge_clearance": p["edge_clearance"], "min_hole_clearance": p["hole_clearance"],
        "min_silk_clearance": p["silk_clearance"], "min_text_height": p["min_text_height"],
        "min_text_thickness": p["min_text_thickness"], "allow_blind_buried_vias": False,
        "allow_microvias": False, "min_microvia_diameter": 0.2, "min_microvia_drill": 0.1,
        "min_resolved_spokes": 2, "min_connection": 0.0, "max_error": 0.005,
        "solder_mask_to_copper_clearance": 0.0, "use_height_for_length_calcs": True,
    })
    has_pats = any(x.get("netclass") == "Power"
                   for x in (pro.get("net_settings") or {}).get("netclass_patterns") or [])
    nc = set_netclasses(pr, default_track=default_track, power_track=power_track, via=via, power_via=power_via,
                        extra_power=extra_power, default_clearance=default_clearance, preset=preset,
                        replace_power_patterns=(power_patterns if power_patterns is not None
                                                else (None if has_pats else POWER_PATTERNS)),
                        pro=pro, write=False)
    default_track, default_clearance = nc["Default"]["track"], nc["Default"]["clearance"]
    power_track, via, power_via = nc["Power"]["track"], nc["Default"]["via"], nc["Power"]["via"]
    widths = sorted({0.0, default_track, power_track, 0.15 if p["min_track"] <= 0.15 else default_track,
                     0.25, 0.3, 0.5, 0.8, 1.0})
    ds["track_widths"] = [w for w in widths if w == 0 or w >= p["min_track"]]
    ds["via_dimensions"] = [{"diameter": 0.0, "drill": 0.0}, {"diameter": via[0], "drill": via[1]},
                            {"diameter": power_via[0], "drill": power_via[1]}]
    defaults = ds.setdefault("defaults", {})
    defaults.update({"silk_text_size_h": silk_text, "silk_text_size_v": silk_text,
                     "silk_text_thickness": silk_thickness, "silk_line_width": max(0.15, silk_thickness),
                     "board_outline_line_width": 0.05, "copper_line_width": default_track})
    defaults.setdefault("zones", {})["min_clearance"] = max(0.3, default_clearance)
    pro.setdefault("meta", {"filename": pr.pro.name, "version": 3})
    projmod.atomic_write(pr.pro, json.dumps(pro, indent=2) + "\n")
    dru = write_dru(pr, preset)
    return {"preset": preset, "label": p["label"], "rules": {k: v for k, v in rules.items() if k.startswith("min_")},
            "netclasses": {"Default": nc["Default"],
                           "Power": dict(nc["Power"], patterns=len(nc["Power"]["patterns"]))},
            "power_patterns": nc["Power"]["patterns"], "dru": str(dru)}


BEGIN, END = "# --- copper:begin (managed by Copper; edits inside are overwritten) ---", "# --- copper:end ---"


def dru_rules(preset: str, pr=None) -> str:
    p = _require_preset(preset, pr)
    r = [
        f'(rule "Copper {preset}: track width"\n\t(condition "A.Type == \'track\' || A.Type == \'arc\'")\n'
        f'\t(constraint track_width (min {p["min_track"]}mm)))',
        f'(rule "Copper {preset}: copper clearance"\n\t(constraint clearance (min {p["min_clearance"]}mm)))',
        f'(rule "Copper {preset}: via size"\n\t(condition "A.Type == \'via\'")\n'
        f'\t(constraint hole_size (min {p["min_via_drill"]}mm))\n\t(constraint via_diameter (min {p["min_via_diameter"]}mm))\n'
        f'\t(constraint annular_width (min {p["min_annular"]}mm)))',
        f'(rule "Copper {preset}: PTH annular ring"\n\t(condition "A.Type == \'pad\' && A.isPlated()")\n'
        f'\t(constraint annular_width (min {p.get("pth_annular", 0.15)}mm)))',
        f'(rule "Copper {preset}: min drill"\n\t(constraint hole_size (min {p["min_hole"]}mm)))',
        f'(rule "Copper {preset}: hole to hole"\n\t(constraint hole_to_hole (min {p["hole_to_hole"]}mm)))',
        f'(rule "Copper {preset}: board edge"\n\t(constraint edge_clearance (min {p["edge_clearance"]}mm)))',
        # tracks/vias/zones vs holes only — pad↔peg spacing inside a footprint is the footprint's own design
        f'(rule "Copper {preset}: hole to copper"\n\t(condition "A.Type != \'Pad\' || B.Type != \'Pad\'")\n'
        f'\t(constraint hole_clearance (min {p["hole_clearance"]}mm)))',
        f'(rule "Copper {preset}: silkscreen text"\n\t(layer "?.Silkscreen")\n'
        f'\t(constraint text_height (min {p["min_text_height"]}mm))\n\t(constraint text_thickness (min {p["min_text_thickness"]}mm)))',
    ]
    return "\n\n".join(r)


def write_dru(pr, preset: str) -> Path:
    path = pr.dir / f"{pr.name}.kicad_dru"
    block = BEGIN + "\n" + dru_rules(preset, pr) + "\n" + END
    if path.exists():
        cur = path.read_text(encoding="utf-8")
        if BEGIN in cur and END in cur:
            new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda m: block, cur, flags=re.S)
        else:
            body = cur.strip()
            if not body.startswith("(version"):
                body = "(version 1)\n" + body
            new = body + "\n\n" + block + "\n"
    else:
        new = "(version 1)\n\n" + block + "\n"
    projmod.atomic_write(path, new)
    return path
