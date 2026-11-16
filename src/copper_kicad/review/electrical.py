"""Rule-based electrical checks: what a datasheet says, applied to the netlist.

    cap_voltage   capacitor rated voltage vs the rail it sits on (1.5x ceramic / 1.25x polarized)
    tvs_clamp     TVS stand-off below its rail, or clamp voltage above the abs-max of the ICs it protects
    ic_supply     IC supply pins on a rail outside the part's operating range / abs-max
    joined_pins   switch pins joined inside the part (tact switch 1-2 / 3-4) placed on two different nets
    mcu_pins      STM32 SWD/SWO pins shared with another function; STM32G4 PB8 = BOOT0 driven at reset
    (crystal load capacitance lives in design_review.check_crystal_caps, using crystal_load_pf() from here)

Every number comes from review/data/electrical.json, which cites its datasheet. Rail voltages come from, in
order: the user's rail_volts override, the net name (+12V, 3V3, VBAT_20V), a fixed regulator's output (AMS1117-3.3
→ 3.3) and, as an upper bound only, the stand-off of a TVS on the rail. A rail with no known voltage is skipped —
these checks stay quiet unless the numbers prove a problem. Read-only, offline (the bundled JLC table and the
local LCSC cache, never the network), cheap enough to run after every sch_apply.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

from .common import base_net, field_ci, finding, is_auto_net, lcsc_of, natural_key, ref_prefix

DATA = Path(__file__).resolve().parent / "data" / "electrical.json"
CHECK_NAMES = ["cap_voltage", "tvs_clamp", "ic_supply", "joined_pins", "mcu_pins"]
# the subset sch_apply surfaces for the parts a call touched (all netlist-only and offline)
QUICK_TYPES = {"cap_overvoltage", "cap_derating", "tvs_standoff_below_rail", "tvs_clamp_above_abs_max",
               "ic_supply_over_abs_max", "ic_supply_over_max", "ic_supply_no_headroom", "ic_supply_under_min",
               "internally_joined_pins", "mcu_debug_pin_conflict", "mcu_swd_pin_reused", "mcu_boot0_shared",
               "crystal_load_mismatch"}
CAP_SERIES_V = [2.5, 4, 6.3, 10, 16, 25, 35, 50, 63, 100, 160, 200, 250, 400, 450, 630, 1000]
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]


@lru_cache(maxsize=1)
def data() -> dict:
    d = json.loads(DATA.read_text(encoding="utf-8"))
    d["tvs_families"]["_clamp"] = {float(k): v for k, v in d["tvs_families"]["clamp_by_standoff"].items()}
    return d


# ── part facts ─────────────────────────────────────────────────────────────
def _fields(comp: dict) -> dict:
    return comp.get("fields") or {}


def mpn_of(comp: dict) -> str:
    return field_ci(_fields(comp), "MPN", "Manufacturer Part Number", "MFR Part", "Mfr. Part #", "PartNumber") or ""


def part_record(comp: dict) -> dict | None:
    """Offline facts for the part's LCSC number: the bundled JLC table, else the local LCSC cache (no network)."""
    code = lcsc_of(_fields(comp))
    if not code:
        return None
    try:
        from ..parts import basic
        r = basic.by_lcsc(code)
        if r:
            return {"attributes": r.get("attributes") or {}, "typed": r.get("typed") or {}, "kind": r.get("kind"),
                    "subcategory": r.get("subcategory") or "", "description": r.get("description") or ""}
    except Exception:  # noqa: BLE001 — the table is an optional speed-up
        pass
    try:
        from ..parts import common as PC
        hit = PC.cache_get("wmsc", code, None)
        if isinstance(hit, dict):
            params = {p.get("paramNameEn"): p.get("paramValueEn") for p in hit.get("paramVOList") or [] if p.get("paramNameEn")}
            return {"attributes": params, "typed": {}, "kind": None, "subcategory": hit.get("catalogName") or "",
                    "description": hit.get("productIntroEn") or ""}
    except Exception:  # noqa: BLE001
        pass
    return None


def _volts(s) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*V\b", str(s or ""), re.I)
    return float(m.group(1)) if m else None


def part_text(comp: dict) -> str:
    """value + lib_id + MPN + LCSC number, lower case: what the data table's 'match' regexes search."""
    return f"{comp.get('value', '')} {comp.get('lib_id', '')} {mpn_of(comp)} {lcsc_of(_fields(comp))}".lower()


def is_polarized_cap(comp: dict, rec: dict | None = None) -> bool:
    part = (comp.get("lib_id") or "").split(":")[-1].lower()
    t = f"{part} {comp.get('footprint', '')} {comp.get('description', '')} {comp.get('value', '')}".lower()
    if rec and rec.get("kind") in ("electrolytic", "tantalum"):
        return True
    return bool(part in ("cp", "cp_small", "c_polarized", "c_polarized_small", "cpsmall")
                or re.search(r"polarized|cp_elec|cp_tantal|tantal|electrolytic|elec_|polymer|\bcp_", t))


_EIA_ELECTROLYTIC = {"0G": 4.0, "0J": 6.3, "1A": 10.0, "1C": 16.0, "1E": 25.0, "1V": 35.0, "1H": 50.0, "1J": 63.0,
                     "1K": 80.0, "2A": 100.0, "2C": 160.0, "2D": 200.0, "2E": 250.0, "2G": 400.0, "2W": 450.0}


def cap_rating(comp: dict) -> tuple[float, str] | None:
    """(rated volts, where it came from) — Value text ('10uF 25V'), a voltage field, the LCSC record, or the MPN."""
    v = _volts(comp.get("value"))
    if v:
        return v, "value"
    for name in ("Voltage", "Voltage Rating", "Rated Voltage", "Voltage Rated", "Voltage - Rated"):
        v = _volts(field_ci(_fields(comp), name))
        if v:
            return v, f"field {name}"
    rec = part_record(comp)
    if rec:
        if rec["kind"] in ("capacitor", "electrolytic", "tantalum") and rec["typed"].get("V"):
            return float(rec["typed"]["V"]), "LCSC"
        for k in ("Voltage Rating", "Voltage Rated", "Rated Voltage"):
            v = _volts(rec["attributes"].get(k))
            if v:
                return v, "LCSC"
    mpn = mpn_of(comp).upper().replace(" ", "")
    if mpn:
        for fam in data()["mlcc_voltage_codes"].values():
            if not isinstance(fam, dict):
                continue
            mm = re.match(fam["pattern"], mpn)
            if mm and mm.group(1) in fam["codes"]:
                return float(fam["codes"][mm.group(1)]), "MPN code"
        if is_polarized_cap(comp, rec):
            # EIA electrolytic voltage code before the 3-digit capacitance code: Panasonic EEE-FK1V101P,
            # Nichicon UWT1V101MCL1GS → 1V = 35 V (never a literal 1 V)
            # (Panasonic ECA-1HM101: tolerance letter between them)
            mm = re.search(r"(?<![0-9])([0-2][A-Z])(?=[A-Z]?\d{3})", mpn)
            if mm and mm.group(1) in _EIA_ELECTROLYTIC:
                return _EIA_ELECTROLYTIC[mm.group(1)], "MPN code"
            for mm in re.finditer(r"(?<![0-9.])(\d{1,3}(?:\.\d)?)V(?=[0-9A-Z]|$)", mpn):
                if mm.group(1) in ("0", "1", "2"):
                    continue                    # an unlisted EIA-style code, not volts: unknown beats a false error
                if float(mm.group(1)) not in _CAP_VOLTS:
                    continue                    # not a rating capacitors are made in (KEMET T520V107: case V, not 520 V)
                return float(mm.group(1)), "MPN"
    return None


_CAP_VOLTS = {2, 2.5, 4, 6.3, 10, 16, 20, 25, 35, 40, 50, 63, 71, 75, 80, 100, 125, 160, 200, 250, 300, 350, 400, 420, 450}


def tvs_facts(comp: dict) -> dict | None:
    """{'vrwm','clamp'} of a TVS diode (clamp may be None), else None for anything that is not a TVS."""
    rec = part_record(comp)
    t = part_text(comp) + " " + (comp.get("description") or "").lower()
    fam = re.search(data()["tvs_families"]["pattern"], f"{comp.get('value', '')} {mpn_of(comp)}".upper())
    is_tvs = bool(fam) or "tvs" in t or (rec is not None and rec.get("kind") == "esd"
                                          and "Clamping Voltage" in rec["attributes"])
    if not is_tvs:
        return None
    vrwm = clamp = None
    if rec:
        a = rec["attributes"]
        clamp = _volts(a.get("Clamping Voltage"))
        vrwm = _volts(a.get("Reverse Stand-Off Voltage (Vrwm)") or a.get("Reverse Stand-Off Voltage"))
    if fam:
        sv = float(fam.group(2))
        vrwm = vrwm or sv
        clamp = clamp or data()["tvs_families"]["_clamp"].get(sv)
    if vrwm is None and clamp is None:
        return {"vrwm": None, "clamp": None}
    return {"vrwm": vrwm, "clamp": clamp}


def crystal_load_pf(comp: dict) -> float | None:
    """Crystal load capacitance (pF): LCSC record, a 'Load Capacitance' field, or 'xx pF' in Value/MPN/description."""
    rec = part_record(comp)
    if rec:
        if rec["typed"].get("load_pF"):
            return float(rec["typed"]["load_pF"])
        m = re.search(r"(\d+(?:\.\d+)?)\s*pF", str(rec["attributes"].get("Load Capacitance") or ""), re.I)
        if m:
            return float(m.group(1))
    for s in (field_ci(_fields(comp), "Load Capacitance", "CL", "Load"), comp.get("value"), comp.get("description")):
        m = re.search(r"(\d+(?:\.\d+)?)\s*pF", str(s or ""), re.I)
        if m:
            return float(m.group(1))
    return None


def ic_entry(comp: dict) -> dict | None:
    t = part_text(comp)
    for e in data()["ic_supply"]:
        if re.search(e["match"], t, re.I):
            return e
    return None


_IC_PREFIXES = ("U", "IC", "MOD", "A")
_PROTECTION_RE = re.compile(r"\besd\b|\btvs\b|usblc|esda|pesd|prtr|tpd\d|sp05\d|srv05|rclamp|(?:\b|_)protect", re.I)


def is_ic(m, ref: str) -> bool:
    """An IC for these checks: comp_kind 'ic', or a U/IC-designated part comp_kind called a diode only because its
    LCSC description mentions one ('Built-in bootstrap diode' on a gate driver) when the IC table knows the part."""
    k = m.kind.get(ref)
    if k == "ic":
        return True
    return k in ("diode", "other") and ref_prefix(ref) in _IC_PREFIXES and ic_entry(m.comps.get(ref, {})) is not None


def is_protection(comp: dict) -> bool:
    """ESD / TVS arrays and other protection parts: they sit on a signal without using it. The LCSC description
    only counts when it calls the part a TVS/ESD diode or array — many ICs list 'Under Voltage Protection'."""
    return bool(_PROTECTION_RE.search(f"{comp.get('value', '')} {comp.get('lib_id', '')} {mpn_of(comp)}")
                or re.search(r"\b(?:tvs|esd)\b[^,;]{0,30}\b(?:diode|array|suppress)", comp.get("description") or "", re.I))


def _vout(e: dict, comp: dict) -> float | None:
    if e.get("vout"):
        return float(e["vout"])
    if e.get("vout_re"):
        m = re.search(e["vout_re"], part_text(comp), re.I)
        if m:
            return float(m.group(1))
    return None


def pin_tokens(p: dict) -> list[str]:
    n = re.sub(r"~\{([^}]*)\}", r"\1", p.get("pin_name") or "").replace("~", "")
    return [x.strip().upper() for x in re.split(r"[/,\s]|-(?=[A-Z])", n) if x.strip()]


def _pin_spec(e: dict, p: dict) -> dict | None:
    for rx, spec in e["pins"].items():
        if any(re.match(rx, tok, re.I) for tok in pin_tokens(p)):
            return spec
    return None


# ── rail voltages ──────────────────────────────────────────────────────────
def _std_up(v: float, series=CAP_SERIES_V) -> float:
    return next((s for s in series if s >= v - 1e-9), series[-1])


def _e12_near(pf: float) -> float:
    if pf <= 0:
        return 0
    dec = 10 ** math.floor(math.log10(pf))
    return min((x * dec for x in E12 + [10.0]), key=lambda c: abs(c - pf))


def _override_for(net: str, ov: dict) -> float | None:
    if not ov:
        return None
    for k, v in ov.items():
        if k == net or base_net(k) == base_net(net):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def rails(m) -> dict:
    """net → (volts, how). how: 'ground' | 'override' | 'name' | 'regulator' | 'tvs_bound' (an upper bound only)."""
    if getattr(m, "_rails", None) is not None:
        return m._rails
    from .design_review import _rail_volts
    ov = getattr(m, "rail_override", None) or {}
    out: dict = {}
    for net, nodes in m.nets.items():
        if net.startswith("unconnected-"):
            continue
        o = _override_for(net, ov)
        if o is not None:
            out[net] = (o, "override")
        elif net in m.gnd:
            out[net] = (0.0, "ground")
        elif net in m.power and not is_auto_net(net):
            b = base_net(net).upper()
            v = _rail_volts(net)
            if v is not None and b.startswith("-"):
                v = -v                          # '-12V' is 12 V below ground
            if v is not None and re.fullmatch(r"\+?VBUS\w*", b) and not re.fullmatch(r"\+?(?:VBUS|VBUS_?USB|USB_?VBUS|VBUS5V?)", b) \
                    and not re.search(r"\d+V\d*(?![0-9])", b):
                # VBUS_M / VBUS_MOTOR is a motor bus, not USB: 5 V only when a connector's VBUS pin is on it
                # (a voltage written in the name — VBUS_24V, VBUS_3V3 — is taken as given)
                usb = any(m.kind.get(n["ref"]) == "connector" and "VBUS" in pin_tokens(n) for n in nodes)
                v = 5.0 if usb else None
            if v is not None:
                out[net] = (v, "name")
    for ref, comp in m.comps.items():           # fixed regulator outputs name their rail
        if not is_ic(m, ref) or comp.get("dnp"):
            continue
        e = ic_entry(comp)
        if not e or not e.get("out_pins"):
            continue
        vo = _vout(e, comp)
        if vo is None:
            continue
        for p in m.comp_pins.get(ref, []):
            if any(re.match(e["out_pins"], t, re.I) for t in pin_tokens(p)) and p["net"] not in out \
                    and not p["net"].startswith("unconnected-"):
                out[p["net"]] = (vo, "regulator")
    for ref, pins in m.comp_pins.items():       # a TVS's stand-off bounds a rail nobody named
        if m.kind.get(ref) != "diode" or len(pins) != 2 or m.comps[ref].get("dnp"):
            continue
        nets = [p["net"] for p in pins]
        if not any(n in m.gnd for n in nets):
            continue
        tv = tvs_facts(m.comps[ref])
        if not tv or not tv.get("vrwm"):
            continue
        for n in nets:
            if n not in m.gnd and n not in out and not n.startswith("unconnected-"):
                out[n] = (tv["vrwm"], "tvs_bound")
    m._rails = out
    return out


def _vdesc(v: float, how: str) -> str:
    return {"override": f"{v:g} V (rail_volts)", "name": f"{v:g} V (from its name)", "regulator": f"{v:g} V (regulator output)",
            "tvs_bound": f"≤{v:g} V (estimated from the TVS stand-off on it)", "ground": "0 V"}.get(how, f"{v:g} V")


# ── a) capacitor voltage derating ─────────────────────────────────────────
def check_cap_voltage(m) -> list[dict]:
    out, unknown = [], []
    d = data()["derating"]
    rv = rails(m)
    for ref in sorted(m.comps, key=natural_key):
        pins = m.comp_pins.get(ref, [])
        comp = m.comps[ref]
        if m.kind.get(ref) != "capacitor" or len(pins) != 2 or comp.get("dnp"):
            continue
        a, b = (rv.get(p["net"]) for p in pins)
        if a is None or b is None:
            continue
        across = abs(a[0] - b[0])
        if across <= 0:
            continue
        hot = max((a, b), key=lambda x: abs(x[0]))
        hot_net = next(p["net"] for p in pins if rv.get(p["net"]) == hot)
        est = "tvs_bound" in (a[1], b[1])
        r = cap_rating(comp)
        if r is None:
            if across > 6.3 / d["ceramic"]:
                unknown.append((ref, across))
            continue
        rated, src = r
        polar = is_polarized_cap(comp, part_record(comp))
        k = d["polarized"] if polar else d["ceramic"]
        need = k * across
        if rated >= need:
            continue
        want = _std_up(need)
        kind = "polarized" if polar else "ceramic"
        fix = (f"use a ≥{want:g} V part (≥{k:g}× the {across:g} V across it): parts_search(query='{comp.get('value', '')} {want:g}V') "
               f"then sch_apply(ops=[{{\"op\":\"swap\",\"ref\":\"{ref}\",\"lcsc\":\"C…\"}}]) — or set the rating in Value "
               f"('{comp.get('value', '').split()[0] if comp.get('value') else ''} {want:g}V') if the part already is one")
        if rated < across:
            out.append(finding("warning" if est else "error", "cap_overvoltage",
                               f"{ref} ({comp.get('value')}, rated {rated:g} V from {src}) sits on {base_net(hot_net)} at "
                               f"{_vdesc(hot[0], hot[1])}: above its rating", fix=fix, items=[ref], net=hot_net))
        else:
            out.append(finding("warning", "cap_derating",
                               f"{ref} ({comp.get('value')}, {kind}, rated {rated:g} V from {src}) on {base_net(hot_net)} at "
                               f"{_vdesc(hot[0], hot[1])}: below the {k:g}× derating ({need:.3g} V)", fix=fix, items=[ref],
                               net=hot_net))
    if unknown:
        refs = [r for r, _ in unknown]
        out.append(finding("info", "cap_voltage_unknown",
                           f"{len(refs)} capacitor(s) on rails above {6.3 / d['ceramic']:.1f} V have no known voltage rating: "
                           f"{', '.join(refs[:12])}{' …' if len(refs) > 12 else ''}",
                           fix="give each an LCSC number (sch_apply set_field LCSC) or put the rating in Value, e.g. '10uF 25V'",
                           items=refs[:40]))
    return out


# ── b) TVS stand-off / clamp vs the ICs it protects ───────────────────────
def check_tvs_clamp(m) -> list[dict]:
    out = []
    rv = rails(m)
    for ref in sorted(m.comps, key=natural_key):
        pins = m.comp_pins.get(ref, [])
        comp = m.comps[ref]
        if m.kind.get(ref) != "diode" or len(pins) != 2 or comp.get("dnp"):
            continue
        nets = [p["net"] for p in pins]
        if not any(n in m.gnd for n in nets):
            continue
        rail = next((n for n in nets if n not in m.gnd), None)
        tv = tvs_facts(comp)
        if not rail or not tv:
            continue
        v = rv.get(rail)
        if v and v[1] != "tvs_bound" and tv.get("vrwm") and tv["vrwm"] < abs(v[0]):
            out.append(finding("error", "tvs_standoff_below_rail",
                               f"TVS {ref} ({comp.get('value')}, stand-off {tv['vrwm']:g} V) is on {base_net(rail)} at "
                               f"{_vdesc(*v)}: it conducts in normal operation and will overheat",
                               fix=f"pick a TVS whose stand-off (Vrwm) ≥ the rail's maximum: parts_search(query='SMBJ{_std_up(abs(v[0]), [5, 6, 6.5, 7, 7.5, 8, 8.5, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 22, 24, 26, 28, 30, 33, 36, 40, 43, 45, 48, 51, 54, 58, 60, 64]):g}A') then swap {ref}",
                               items=[ref], net=rail))
        clamp = tv.get("clamp")
        if not clamp or (v and v[0] < 0):
            continue                            # negative rails: no negative-supply ICs in the table
        # the lowest clamp a TVS that can still stand off this rail reaches (SMxJ family table)
        # (a rail known only from this TVS's own stand-off may really be lower: any stand-off could do)
        floor = abs(v[0]) if v and v[1] != "tvs_bound" else 0
        reach = [c for so, c in data()["tvs_families"]["_clamp"].items() if so >= floor - 1e-9]
        best_clamp = min(reach) if reach else None
        for ic in [r for r in m.comps_on(rail) if is_ic(m, r)]:
            e = ic_entry(m.comps[ic])
            if not e or m.comps[ic].get("dnp"):
                continue
            for p in m.comp_pins.get(ic, []):
                if p["net"] != rail:
                    continue
                spec = _pin_spec(e, p)
                if not spec or not spec.get("abs_max") or clamp <= spec["abs_max"]:
                    continue
                if best_clamp is None or spec["abs_max"] < best_clamp:
                    # no SMxJ part that stands off this rail clamps this low: the IC rides on series impedance / its own ESD cells — advice, not an error
                    out.append(finding("info", "tvs_clamp_above_abs_max",
                                       f"TVS {ref} ({comp.get('value')}) clamps {base_net(rail)} at {clamp:g} V at full surge "
                                       f"current, above {ic} ({m.comps[ic].get('value')}) {p.get('pin_name') or p['pin']} absolute "
                                       f"maximum {spec['abs_max']:g} V ({e['name']}); usual for a low-voltage rail, where the IC "
                                       f"relies on series impedance and its own ESD structures",
                                       fix=("for hard surges add a series resistor/ferrite or a low-voltage TVS/ESD array "
                                            f"(parts_search(query='ESD TVS {spec['abs_max']:g}V'))"),
                                       items=[ref, f"{ic}.{p['pin']}"], net=rail, source=e.get("source")))
                    continue
                out.append(finding("error", "tvs_clamp_above_abs_max",
                                   f"TVS {ref} ({comp.get('value')}) clamps {base_net(rail)} at {clamp:g} V, above {ic} "
                                   f"({m.comps[ic].get('value')}) {p.get('pin_name') or p['pin']} absolute maximum {spec['abs_max']:g} V "
                                   f"({e['name']}): a surge the TVS absorbs still destroys {ic}",
                                   fix=(f"choose a TVS with clamp < {spec['abs_max']:g} V (lower stand-off, still ≥ the rail's max), "
                                        f"or an IC with a higher input rating; parts_search(query='TVS clamp {spec['abs_max']:g}V') then swap {ref}"),
                                   items=[ref, f"{ic}.{p['pin']}"], net=rail, source=e.get("source")))
    return out


# ── c) IC supply range ─────────────────────────────────────────────────────
def check_ic_supply(m) -> list[dict]:
    out = []
    head = data()["derating"]["headroom_warn"]
    rv = rails(m)
    for ref in sorted(m.comps, key=natural_key):
        comp = m.comps[ref]
        if not is_ic(m, ref) or comp.get("dnp"):
            continue
        e = ic_entry(comp)
        if not e:
            continue
        seen = set()
        for p in sorted(m.comp_pins.get(ref, []), key=lambda q: natural_key(q["pin"])):
            spec = _pin_spec(e, p)
            v = rv.get(p["net"])
            if not spec or not v or v[1] in ("tvs_bound", "ground") or v[0] < 0 or (p["net"], id(spec)) in seen:
                continue
            seen.add((p["net"], id(spec)))
            volts = v[0]
            lo = spec.get("min")
            if lo is None and e.get("dropout") and _vout(e, comp):
                lo = round(_vout(e, comp) + e["dropout"], 2)
            hi, ab = spec.get("max"), spec.get("abs_max")
            pname = p.get("pin_name") or p["pin"]
            where = f"{ref} ({comp.get('value')}) {pname} on {base_net(p['net'])} at {_vdesc(*v)}"
            rng = f"{e['name']}: {lo if lo is not None else '?'}–{hi if hi is not None else '?'} V" + (f", abs max {ab:g} V" if ab else "")
            fix = (f"feed {ref}.{pname} from a rail inside {lo if lo is not None else '…'}–{hi if hi is not None else '…'} V "
                   f"(e.g. a regulator/LDO) — sch_apply(ops=[{{\"op\":\"disconnect\",\"pin\":\"{ref}.{p['pin']}\"}},"
                   f"{{\"op\":\"connect\",\"pin\":\"{ref}.{p['pin']}\",\"net\":\"<rail>\"}}]); if the rail's voltage is wrong, "
                   f"design_review(rail_volts={{\"{base_net(p['net'])}\": <volts>}})")
            if ab and volts > ab:
                out.append(finding("error", "ic_supply_over_abs_max", f"{where}: above the absolute maximum ({rng})",
                                   fix=fix, items=[f"{ref}.{p['pin']}"], net=p["net"], source=e.get("source")))
            elif hi and volts > hi:
                out.append(finding("error", "ic_supply_over_max", f"{where}: above the operating range ({rng})",
                                   fix=fix, items=[f"{ref}.{p['pin']}"], net=p["net"], source=e.get("source")))
            elif hi and volts > head * hi:
                out.append(finding("warning", "ic_supply_no_headroom",
                                   f"{where}: at the top of the operating range ({rng}) — no margin for ripple, charging or transients",
                                   fix=fix, items=[f"{ref}.{p['pin']}"], net=p["net"], source=e.get("source")))
            elif lo is not None and volts < lo:
                out.append(finding("warning", "ic_supply_under_min", f"{where}: below the minimum ({rng})",
                                   fix=fix, items=[f"{ref}.{p['pin']}"], net=p["net"], source=e.get("source")))
    return out


# ── e) pins joined inside the part ────────────────────────────────────────
def _joined_pairs(m, ref: str) -> tuple[list, str, bool] | None:
    """Only a symbol that draws every terminal of the pairs as its own pin can put a pair on two nets: a 2-pin
    SW_Push on the same 4-pad part (pads 1,1,2,2) already joins them, whatever its LCSC number says."""
    comp = m.comps[ref]
    t = part_text(comp)
    have = sorted({p["pin"] for p in m.comp_pins.get(ref, [])}, key=natural_key)
    for e in data()["joined_pins"]:
        if sorted({p for pr in e["pairs"] for p in pr}, key=natural_key) != have:
            continue
        if (e.get("match_lib") and re.search(e["match_lib"], comp.get("lib_id") or "", re.I)) or \
                (e.get("match") and re.search(e["match"], t, re.I)):
            return e["pairs"], e["name"], True
    g = data()["tactile_generic"]
    if g.get("not_lib") and re.search(g["not_lib"], comp.get("lib_id") or "", re.I):
        return None                             # KiCad's DPST SW_Push_Dual: 1-2 / 3-4 are the switched pairs
    nums = sorted(p["pin"] for p in m.comp_pins.get(ref, []))
    if m.kind.get(ref) == "switch" and nums == ["1", "2", "3", "4"]:
        rec = part_record(comp)
        if re.search(g["footprint"], comp.get("footprint") or "", re.I) or (rec and g["subcategory"].lower() in rec["subcategory"].lower()):
            return g["pairs"], "4-pin tactile switch (assumed 1-2 / 3-4)", False
    return None


def check_joined_pins(m) -> list[dict]:
    out = []
    for ref in sorted(m.comps, key=natural_key):
        k = m.kind.get(ref)
        if m.comps[ref].get("dnp") or not (k in ("switch", "other")
                                           or (k == "connector" and ref_prefix(ref) in ("SW", "S", "BTN", "K"))):
            continue
        jp = _joined_pairs(m, ref)
        if not jp:
            continue
        pairs, name, known = jp
        net = {p["pin"]: p["net"] for p in m.comp_pins.get(ref, []) if not p["net"].startswith("unconnected-")}
        bad = [(a, b) for a, b in pairs if a in net and b in net and net[a] != net[b]]
        if not bad:
            continue
        # the fix: each joined pair on ONE net — pair 0 keeps its first pin's net, pair 1 takes the switch's other net
        allnets = list(dict.fromkeys(net[p] for pr in pairs for p in pr if p in net))
        target = {}
        if len(pairs) == 2 and len(allnets) == 2:
            n0 = net.get(pairs[0][0]) or allnets[0]
            n1 = next(n for n in allnets if n != n0)
            target = {p: n0 for p in pairs[0]} | {p: n1 for p in pairs[1]}
        moves = [p for p, n in target.items() if p in net and net[p] != n]
        fix = ("put both pins of each joined pair on the same net"
               + (": sch_apply(ops=[" + ",".join(
                   [f'{{"op":"disconnect","pins":{json.dumps([f"{ref}.{p}" for p in moves])}}}']
                   + [f'{{"op":"connect","pin":"{ref}.{p}","net":"{base_net(target[p])}"}}' for p in moves]) + "])" if moves else ""))
        desc = "; ".join(f"pins {a} and {b} are joined inside the part but sit on {base_net(net[a])} and {base_net(net[b])}"
                         for a, b in bad)
        shorted = sorted({base_net(net[x]) for a, b in bad for x in (a, b)})
        out.append(finding(
            "error" if known else "warning", "internally_joined_pins",
            f"{ref} ({m.comps[ref].get('value')}, {name}): {desc} — {' and '.join(shorted)} are shorted permanently, "
            f"pressed or not", fix=fix, items=[f"{ref}.{x}" for a, b in bad for x in (a, b)], net=net[bad[0][0]]))
    return out


# ── f) STM32 pin conflicts ────────────────────────────────────────────────
def _pin_by_token(m, ref: str, token: str) -> dict | None:
    for p in m.comp_pins.get(ref, []):
        if token in pin_tokens(p):
            return p
    return None


def check_mcu_pins(m) -> list[dict]:
    out = []
    st = data()["stm32"]
    dbg_re = re.compile(rf"(?:^|[_\W])(?:{st['debug_net_re']})(?:$|[_\W\d])", re.I)
    for ref in sorted(m.comps, key=natural_key):
        comp = m.comps[ref]
        if not is_ic(m, ref) or comp.get("dnp") or not re.search(st["match"], part_text(comp)):
            continue

        def conns(net):
            return {n["ref"] for n in m.nets.get(net, []) if m.kind.get(n["ref"]) == "connector"}
        dbg_conn = set()                    # connectors carrying SWD nets = the debug header
        for p in m.comp_pins.get(ref, []):
            if any(t in st["debug_pins"] for t in pin_tokens(p)) and dbg_re.search(base_net(p["net"])):
                dbg_conn |= conns(p["net"])
        # a header wired with plain wires (Net-(J1-Pin_2)) or nets named after the pins (PA13/PA14): the
        # connector that carries BOTH SWD pins is the debug header
        swd = [_pin_by_token(m, ref, t) for t in st["essential"]]
        if all(q and not q["net"].startswith("unconnected-") for q in swd) and len({q["net"] for q in swd}) == len(swd):
            both = set.intersection(*(conns(q["net"]) for q in swd))
            dbg_conn |= both
        for tok, func in st["debug_pins"].items():
            p = _pin_by_token(m, ref, tok)
            if not p or p["net"].startswith("unconnected-"):
                continue
            net = p["net"]
            # a net named after the pin itself (PA13, U1_PA13) says nothing about its function
            pin_named = bool(re.search(rf"(?:^|[_\W]){tok}(?:$|[_\W])", base_net(net), re.I))
            named_dbg = (not is_auto_net(net)) and bool(dbg_re.search(base_net(net)))
            named_other = (not is_auto_net(net)) and not named_dbg and not pin_named
            others = [n for n in m.nets.get(net, []) if n["ref"] != ref]
            active = [f"{n['ref']}.{n.get('pin_name') or n['pin']}" for n in others
                      if not is_protection(m.comps.get(n["ref"], {})) and (
                          m.kind.get(n["ref"]) in ("ic", "transistor", "oscillator")
                          or (m.kind.get(n["ref"]) == "connector" and n["ref"] not in dbg_conn))]
            debug_use = named_dbg or any(n["ref"] in dbg_conn for n in others)
            other_use = named_other or bool(active)
            if debug_use and other_use:
                out.append(finding(
                    "warning", "mcu_debug_pin_conflict",
                    f"{ref} {tok} is {func} (debug) and is also used as {base_net(net)}"
                    f"{' by ' + ', '.join(active[:4]) if active else ''}: the debugger and the other function fight over the pin",
                    fix=(f"move {base_net(net)} to a free GPIO (sch_apply disconnect {ref}.{p['pin']} + connect the new pin), "
                         f"or drop the {func} wire to the debug header"),
                    items=[f"{ref}.{p['pin']}"] + active[:4], net=net))
            elif other_use and tok in st["essential"]:
                out.append(finding(
                    "warning", "mcu_swd_pin_reused",
                    f"{ref} {tok} ({func}) is used as {base_net(net)}: SWD programming/debug is lost once firmware "
                    f"reconfigures it (recovery needs connect-under-reset on NRST)",
                    fix=f"keep {tok} for {func} and move {base_net(net)} to a free GPIO; wire {func} to the debug header",
                    items=[f"{ref}.{p['pin']}"] + active[:4], net=net))
        t = part_text(comp)
        for b in st["boot0_shared"]:
            if not re.search(b["family"], t):
                continue
            p = _pin_by_token(m, ref, b["pin"])
            if not p or p["net"].startswith("unconnected-"):
                continue
            net = p["net"]
            drivers = []
            for n in m.nets.get(net, []):
                if n["ref"] == ref or is_protection(m.comps.get(n["ref"], {})):
                    continue
                k = m.kind.get(n["ref"])
                if k in ("ic", "transistor", "oscillator") or (k == "connector" and n["ref"] not in dbg_conn):
                    drivers.append(f"{n['ref']}.{n.get('pin_name') or n['pin']}")
                elif k == "resistor":
                    other = [q["net"] for q in m.comp_pins.get(n["ref"], []) if q["net"] != net]
                    if other and other[0] in m.power:
                        drivers.append(f"{n['ref']} (pull-up to {base_net(other[0])})")
            if not drivers:
                continue
            out.append(finding(
                "warning", "mcu_boot0_shared",
                f"{ref} {b['pin']} is also BOOT0 on this family; {base_net(net)} is driven by {', '.join(drivers[:4])} — "
                f"if it is high at reset the chip boots the ROM bootloader instead of your firmware",
                fix=(f"program the option bytes nSWBOOT0=0 and nBOOT0=1 (STM32CubeProgrammer › Option bytes › User configuration) "
                     f"so BOOT0 comes from the option bit and {b['pin']} is a plain GPIO; or move {base_net(net)} to another pin"),
                items=[f"{ref}.{p['pin']}"] + drivers[:4], net=net, source=b.get("source")))
    return out


TABLE = [("cap_voltage", check_cap_voltage), ("tvs_clamp", check_tvs_clamp), ("ic_supply", check_ic_supply),
         ("joined_pins", check_joined_pins), ("mcu_pins", check_mcu_pins)]


# ── sch_apply's post-write pass ───────────────────────────────────────────
def touched_refs(ops, refs_map: dict | None) -> set:
    """References a sch_apply batch placed or wired: placed parts (via the id → ref map) and every REF in a pin
    argument or `ref` field. Temporary ids resolve through refs_map."""
    refs_map = refs_map or {}
    out = set(v for v in refs_map.values() if isinstance(v, str))

    def add(x):
        if isinstance(x, str) and x:
            r = x.split(".")[0]
            out.add(refs_map.get(r, r))
    for op in ops if isinstance(ops, list) else []:
        if not isinstance(op, dict):
            continue
        for k in ("ref", "pin", "from", "to"):
            add(op.get(k))
        for x in op.get("pins") or []:
            add(x)
    return out


def quick_check(nl: dict, touched: set, rail_volts: dict | None = None, limit: int = 8) -> dict | None:
    """The cheap electrical checks on a netlist sch_apply already exported, filtered to findings about the parts
    the call touched (errors and warnings only). None when there is nothing to say."""
    from .design_review import _Model, check_crystal_caps
    m = _Model(nl)
    m.rail_override = rail_volts or {}
    fs = []
    for _, fn in TABLE + [("crystal_caps", check_crystal_caps)]:
        try:
            fs += fn(m)
        except Exception:  # noqa: BLE001 — advisory: never sink a write
            continue
    keep = []
    for f in fs:
        if f["severity"] not in ("error", "warning") or f["type"] not in QUICK_TYPES:
            continue
        refs = {str(i).split(".")[0].split(" ")[0] for i in f.get("items") or []}
        if refs & touched:
            keep.append({k: f[k] for k in ("severity", "type", "message", "fix", "items") if f.get(k)})
    if not keep:
        return None
    keep.sort(key=lambda f: (f["severity"] != "error", f["type"]))
    d = {"errors": sum(1 for f in keep if f["severity"] == "error"),
         "warnings": sum(1 for f in keep if f["severity"] == "warning"), "findings": keep[:limit]}
    if len(keep) > limit:
        d["more"] = len(keep) - limit
    return d
