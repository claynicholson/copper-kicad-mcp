"""ERC / DRC through kicad-cli, normalised into actionable findings.

Contract (docs/ARCHITECTURE.md):
    run_erc(pr) -> {'errors','warnings','violations':[finding],'report':path, ...}
    run_drc(pr, refill=True, parity=True) -> {...}
    erc_brief(pr, limit=12) -> counts + top findings (cheap enough after every mutation)

A finding is {'severity','type','message','items':[...],'at':[x,y] mm,'fix':...}.
Results are stored in <project>/.copper/verify.json so every run reports
what is new and what got fixed since the previous run.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .. import sexpr
from ..kicad import env
from ..mcp.registry import ToolError
from ..project import revision
from .common import (base_net, cli, work_dir, finding, load_netlist, load_state, now, parse_item, save_state, sort_findings)

# ERC types that usually mean two different signals got shorted → escalate to error
ESCALATE_ERC = {
    "multiple_net_names": "two different net names are attached to one net — usually an unintended short (e.g. ISENSE tied to ISET)",
    "same_local_global_label": "a local and a global label share a name but are different nets — almost always a mistake",
}
# Informational ERC/DRC types that never block anything on their own
SOFT_TYPES = {"endpoint_off_grid", "lib_symbol_mismatch", "lib_footprint_mismatch", "footprint_filter",
              "simulation_model_issue", "four_way_junction", "silk_overlap", "silk_over_copper",
              "silk_edge_clearance", "text_height", "text_thickness", "unresolved_variable"}


# ── helpers ───────────────────────────────────────────────────────────────
def _q(s) -> str:
    return json.dumps(s, ensure_ascii=False)


def _sig(v: dict) -> str:
    # power symbols / PWR_FLAGs are re-numbered whenever a sheet is re-drawn: their number is not an identity
    descs = sorted(re.sub(r"#(PWR|FLG)\w*", r"#\g<1>", re.sub(r"length [\d.]+ ?mm", "", i.get("description", "")))
                   for i in v.get("items", []))
    desc = v.get("description", "")
    if v.get("type") == "multiple_net_names":
        m = re.match(r"Both (.+?) and (.+?) are attached", desc)
        if m:                                    # KiCad 9/10 wording: the two names are in the description itself
            return "multiple_net_names|" + "|".join(sorted(g.strip("'\"") for g in m.groups()))
        # KiCad picks which name/item to report non-deterministically: sign by the SET of quoted net names only
        # (item brackets carry pin info of whichever item was picked, which made the signature unstable)
        names = sorted(set(re.findall(r"'([^']+)'", desc + " " + " ".join(descs))))
        return "multiple_net_names|" + "|".join(names)
    return v.get("type", "") + "|" + desc[:120] + "|" + "|".join(descs)


def _lib_name(desc: str) -> str | None:
    m = re.search(r"library '([^']+)'", desc or "")
    return m.group(1) if m else None


class _Ctx:
    """Lazy per-run context: pin → net lookup from the netlist (only built
    when some finding actually needs it)."""

    def __init__(self, pr):
        self.pr = pr
        self._pin_net = None
        self._pwr = None

    def net_of(self, pin_ref: str) -> str | None:
        if self._pin_net is None:
            self._pin_net = {}
            try:
                nl = load_netlist(self.pr)
                for name, nodes in nl["nets"].items():
                    for n in nodes:
                        self._pin_net[f"{n['ref']}.{n['pin']}"] = name
            except Exception:  # noqa: BLE001 — hints only, never fail the check
                pass
        if pin_ref in self._pin_net:
            return self._pin_net[pin_ref]
        # power symbols (#PWR…) are not in the netlist: their Value *is* the net name
        ref = pin_ref.split(".")[0]
        if ref.startswith("#"):
            return self.power_value(ref)
        return None

    def power_value(self, ref: str) -> str | None:
        if self._pwr is None:
            self._pwr = {}
            try:
                for f in self.pr.sheets():
                    tree = sexpr.loads(f.read_text(encoding="utf-8"))
                    for s in sexpr.find_all(tree, "symbol"):
                        props = {str(p[1]): str(p[2]) for p in sexpr.find_all(s, "property") if len(p) > 2}
                        refs = {props.get("Reference", "")}
                        for inst in sexpr.find_all(s, "instances"):
                            for prj in sexpr.find_all(inst, "project"):
                                for path in sexpr.find_all(prj, "path"):
                                    refs.add(str(sexpr.value(path, "reference", "")))
                        for r in refs:
                            if r.startswith("#"):
                                self._pwr[r] = props.get("Value", "")
            except Exception:  # noqa: BLE001
                pass
        return self._pwr.get(ref) or None


# ── fix hints ─────────────────────────────────────────────────────────────
def _erc_fix(t: str, items: list[dict], desc: str, ctx: _Ctx) -> str:
    pins = [i for i in items if i.get("kind") == "pin"]
    p0 = pins[0] if pins else {}
    pin = p0.get("pin_ref", "REF.PIN")
    pname = f" ({p0['pin_name']})" if p0.get("pin_name") else ""
    labels = [i for i in items if i.get("kind") == "label"]
    if t == "pin_not_connected":
        if str(p0.get("pin_ref", "")).startswith("#"):
            # a power symbol / PWR_FLAG is not a part: its pin can't be wired "to a net" (it IS the net) and
            # sch_apply refuses no_connect on it — the only fix is to remove the stray symbol
            sym = pin.split(".")[0]
            what = "PWR_FLAG" if sym.startswith("#FLG") else "power symbol"
            return (f"{sym} is a {what} that touches nothing (left behind by a deleted or moved part) — remove it: "
                    f"sch_apply(ops=[{{\"op\":\"delete\",\"ref\":\"{sym}\"}}])")
        return (f"sch_apply(ops=[{{\"op\":\"connect\",\"pin\":\"{pin}\",\"net\":\"<NET>\"}}]) if {pin}{pname} should be wired; "
                f"if it is intentionally unused: sch_apply(ops=[{{\"op\":\"no_connect\",\"pin\":\"{pin}\"}}])")
    if t == "power_pin_not_driven":
        net = ctx.net_of(pin) if pins else None
        if net and not net.startswith("unconnected-") and net != "PWR_FLAG":
            return (f"rail {_q(net)} has no power source in the schematic. If it comes from a connector/battery/regulator "
                    f"not modelled as a power output, flag it: sch_apply(ops=[{{\"op\":\"pwr_flag\",\"net\":{_q(base_net(net))}}}]); "
                    f"otherwise connect the regulator output pin to {_q(base_net(net))}")
        return (f"{pin}{pname} is a power input with nothing driving it — connect it to its rail: "
                f"sch_apply(ops=[{{\"op\":\"connect\",\"pin\":\"{pin}\",\"net\":\"<RAIL>\"}}]), "
                f"then sch_apply(ops=[{{\"op\":\"pwr_flag\",\"net\":\"<RAIL>\"}}]) if that rail is fed from a connector")
    if t == "pin_not_driven":
        return (f"input {pin}{pname} has no driver on its net — connect it to the signal that drives it, or tie an unused input "
                f"to GND/VCC: sch_apply(ops=[{{\"op\":\"connect\",\"pin\":\"{pin}\",\"net\":\"GND\"}}])")
    if t in ("label_dangling", "global_label_dangling", "isolated_pin_label", "label_single_pin"):
        net = labels[0]["net"] if labels else "<NET>"
        return (f"label {_q(net)} connects to fewer than two pins — connect the other end with "
                f"sch_apply(ops=[{{\"op\":\"connect\",\"pin\":\"REF.PIN\",\"net\":{_q(net)}}}]) or remove it with "
                f"sch_apply(ops=[{{\"op\":\"delete\",\"label\":{_q(net)}}}]) (check the spelling first)")
    if t == "single_global_label":
        net = labels[0]["net"] if labels else "<NET>"
        return f"global label {_q(net)} is used once — connect its partner pin to {_q(net)} or use a local label"
    if t in ("wire_dangling", "unconnected_wire_endpoint"):
        w = next((i for i in items if i.get("kind") == "wire"), {})
        where = f" at {w['at']}" if w.get("at") else ""
        one = (f"; only this segment: sch_apply(ops=[{{\"op\":\"delete\",\"wire_at\":{json.dumps(w['at'])}}}])"
               if w.get("at") else "")
        return (f"a wire end{where} touches nothing — usually a stub left behind by a deleted part. Remove stray wires: "
                f"sch_apply(ops=[{{\"op\":\"delete\",\"dangling\":true}}]){one} — or, if it was meant to reach a pin, "
                f"sch_apply connect that pin to the net instead")
    if t == "no_connect_connected":
        return (f"{pin} has a no-connect flag but is also wired — drop whichever is wrong: "
                f"sch_apply(ops=[{{\"op\":\"disconnect\",\"pin\":\"{pin}\"}}]) then re-add the intended connection")
    if t == "no_connect_dangling":
        return "a no-connect flag is not on any pin — delete the stray flag (direct edit) or move it onto the pin it was meant for"
    if t == "multiple_net_names":
        names = re.findall(r"'([^']+)'", desc)
        nn = " and ".join(names[:2]) if names else "two names"
        return (f"{nn} are the same net. If they should be separate, find the pin wired to both and "
                f"sch_apply(ops=[{{\"op\":\"disconnect\",\"pin\":\"REF.PIN\"}},{{\"op\":\"connect\",\"pin\":\"REF.PIN\",\"net\":\"<right net>\"}}]); "
                f"if they really are one net, keep a single label name")
    if t in ("similar_labels", "similar_power", "similar_label_and_power"):
        return "labels differ only in case/spelling — rename so identical nets match exactly and different nets are clearly distinct"
    if t == "same_local_global_label":
        return "rename the local label or make both global so the intent is unambiguous"
    if t in ("hier_label_mismatch",):
        return "add/rename the sheet pin so it matches the hierarchical label inside the sub-sheet (exact spelling)"
    if t in ("unannotated", "duplicate_reference"):
        return "give each symbol a unique reference: sch_apply(ops=[{\"op\":\"set_field\",\"ref\":\"<REF>\",\"field\":\"Reference\",\"value\":\"<NEW>\"}])"
    if t == "missing_unit":
        return "place the missing unit(s): sch_apply(ops=[{\"op\":\"place\",\"lib_id\":\"<lib:sym>\",\"ref\":\"<REF>\",\"unit\":<n>}])"
    if t == "missing_power_pin":
        return "place the power unit (VCC/GND pins) of the multi-unit symbol: sch_apply place with \"unit\":\"all\""
    if t in ("missing_input_pin", "missing_bidi_pin"):
        return "a unit with input/bidirectional pins is not placed — place it (sch_apply place with unit) and connect or no-connect its pins"
    if t == "lib_symbol_issues":
        lib = _lib_name(desc)
        return (f"symbol library {_q(lib) if lib else ''} is not in the configured lib tables — library(action='repair', dry_run=false) "
                f"restores KiCad's stock tables; for a supplier's part use parts_import(part=…)")
    if t == "lib_symbol_mismatch":
        return "the symbol cached in the schematic differs from the library copy — harmless if intended; refresh with sch_apply swap"
    if t == "footprint_link_issues":
        lib = _lib_name(desc)
        ref = next((i.get("ref") for i in items if i.get("ref")), "REF")
        if "not found in library" in desc or "footprint '" in desc.lower():
            return (f"the footprint name does not exist in library {_q(lib) if lib else ''} — pick a real one with "
                    f"library(action='search_footprints', query='<package>') and set it: "
                    f"sch_apply(ops=[{{\"op\":\"set_field\",\"ref\":\"{ref}\",\"field\":\"Footprint\",\"value\":\"<Library:Footprint>\"}}])")
        return (f"footprint library {_q(lib) if lib else ''} is not in KiCad's library tables — library(action='repair', dry_run=false) restores "
                f"KiCad's stock tables; for a project-local library add it to the project's fp-lib-table; or re-point {ref} with "
                f"sch_apply(ops=[{{\"op\":\"set_field\",\"ref\":\"{ref}\",\"field\":\"Footprint\",\"value\":\"<Library:Footprint>\"}}])")
    if t == "footprint_filter":
        return "the assigned footprint does not match the symbol's footprint filters — double-check pin count/package"
    if t == "endpoint_off_grid":
        return "cosmetic: an item is off the 1.27 mm connection grid — sch_apply move it onto the grid so connections can't be missed"
    if t == "pin_to_pin":
        flags = [i for i in pins if str(i.get("ref", "")).startswith("#FLG")]
        real = [i for i in pins if not str(i.get("ref", "")).startswith("#")]
        if flags and real:
            r0 = real[0]
            net = ctx.net_of(r0.get("pin_ref", "")) or "this net"
            return (f"benign in the usual case: a PWR_FLAG is not a part, it only marks {_q(base_net(net))} as powered, and KiCad's pin "
                    f"matrix flags {r0.get('pin_ref')} ({r0.get('pin_type') or 'signal'}) sharing a net with a power output. Strapping a "
                    f"pin to a rail/ground (e.g. an I2C address pin tied low) is correct — keep it. It is only a real conflict if "
                    f"{r0.get('pin_ref')} actively drives {_q(base_net(net))}. To silence it, exclude this warning in KiCad's ERC dialog")
        return (f"incompatible pins share a net (e.g. two outputs, or output→power): inspect {', '.join(i.get('pin_ref', '') for i in pins) or 'the pins'} — "
                f"this is often a real short; move one pin to its correct net")
    if t == "ground_pin_not_ground":
        return f"{pin}{pname} is a ground pin on a non-ground net — sch_apply(ops=[{{\"op\":\"connect\",\"pin\":\"{pin}\",\"net\":\"GND\"}}])"
    if t == "different_unit_net":
        return "the same pin in different units is on different nets — make them match"
    if t == "different_unit_footprint":
        return "units of one symbol have different footprints — set the same Footprint on all units"
    if t == "unresolved_variable":
        return "a ${VARIABLE} in text is undefined — define it in the project's text_variables or remove it"
    if t.startswith("bus_") or t == "net_not_bus_member":
        return "bus definition issue — check bus labels/aliases match their member nets"
    if t in ("undefined_netclass", "conflicting_netclasses"):
        return "net-class directive names an undefined or conflicting class — fix the class name in project net classes"
    if t == "label_multiple_wires":
        return "a label sits on a junction of several wires — move it onto a single wire end"
    return "inspect it: render(target='schematic', crop=[x-15,y-15,x+15,y+15]) around 'at', then fix with sch_apply"


_COPPER_KINDS = {"track", "via", "arc", "blind_via", "buried_via", "micro_via", "microvia", "blind/buried_via"}


_DEFAULT_FAB = {"who": "JLC", "preset": "jlc", "min_track": 0.127, "via": (0.6, 0.3), "pth_annular": 0.15,
                "hole_to_hole": 0.5, "edge_clearance": 0.3, "min_text_thickness": 0.15}


def _fab_rules(pr) -> dict:
    """The numbers DRC hints quote: the project's fab profile (review/fab_profiles.py), JLCPCB's by default."""
    try:
        from ..pcb import rules as R
        from . import fab_profiles as FP
        prof = FP.active(pr)
        name = R.default_preset(pr)
        p = R.preset_rules(name, pr) or {}
        return {**_DEFAULT_FAB, **{k: p[k] for k in _DEFAULT_FAB if k in p}, "who": prof.get("short") or prof["label"],
                "preset": name}
    except Exception:  # noqa: BLE001 — a hint must never fail a DRC run
        return dict(_DEFAULT_FAB)


def _drc_fix(t: str, items: list[dict], desc: str, fab: dict | None = None) -> str:
    fab = fab or _DEFAULT_FAB
    refs = sorted({i["ref"] for i in items if i.get("ref")})
    nets = sorted({i["net"] for i in items if i.get("net") and not i["net"].startswith("<")})
    r = ", ".join(refs) or "the items"
    if t == "unconnected_items":
        pads = [i["pin_ref"] for i in items if i.get("pin_ref")]
        gnd = any(n.upper().endswith("GND") for n in nets)
        if len(pads) == 2 and not gnd:
            return (f"route it: pcb_route(tracks=[{{\"from\":\"{pads[0]}\",\"to\":\"{pads[1]}\"}}]) — or pcb_route() to autoroute "
                    f"every unrouted connection")
        return "pcb_route() to autoroute every unrouted connection" + (" — or pcb_pour(net='GND') for the ground net" if gnd else "")
    if t in ("clearance", "shorting_items", "tracks_crossing", "hole_clearance"):
        extra = " — SHORT between different nets, fix before ordering" if t in ("shorting_items", "tracks_crossing") else ""
        # name the exact copper of THIS violation; never "delete nets:[GND,…]" (that rips up a whole routed net)
        tr = list(dict.fromkeys(i["uuid"] for i in items if i.get("uuid") and i.get("kind") in _COPPER_KINDS))
        between = f" between {' and '.join(nets)}" if nets else ""
        nudge = (f"nudge {r} with pcb_place(moves=[{{\"ref\":\"{refs[0]}\",\"x\":…,\"y\":…}}])" if refs else None)
        if tr:
            what = "track/via" if len(tr) == 1 else f"{len(tr)} tracks/vias"
            return (f"remove the offending {what}{between} and re-route just that: "
                    f"pcb_edit(ops=[{{\"op\":\"delete\",\"uuids\":{json.dumps(tr)}}}]) then pcb_route()"
                    + (f", or {nudge}" if nudge else "") + extra)
        zone = any(str(i.get("kind", "")).endswith("zone") for i in items)
        if zone:
            return (f"a copper pour is too close{between}: pcb_pour(fill_only=true) to refill it"
                    + (f", or {nudge}" if nudge else "") + extra)
        return (f"no track is involved{between} (pad-to-pad or pad-to-hole): move one part away — "
                + (nudge or "pcb_place(moves=[{\"ref\":\"REF\",\"x\":…,\"y\":…}])") + extra)
    if t == "copper_edge_clearance":
        return (f"keep copper ≥ {fab['edge_clearance']:g} mm ({fab['who']}) from the board edge: move {r} inward with pcb_place "
                f"or re-route the track")
    if t in ("courtyards_overlap", "npth_inside_courtyard", "pth_inside_courtyard"):
        return f"separate {r}: pcb_place(moves=[{{\"ref\":\"{refs[-1] if refs else 'REF'}\",\"x\":…,\"y\":…}}]) so the courtyards no longer overlap"
    if t in ("missing_footprint", "extra_footprint", "footprint_symbol_mismatch", "net_conflict", "duplicate_footprints",
             "schematic_parity", "footprint_filters_mismatch"):
        return "board and schematic disagree — pcb_sync() to push the schematic's footprints, nets and fields to the board"
    if t == "lib_footprint_issues":
        lib = _lib_name(desc)
        return f"footprint library {_q(lib) if lib else ''} is not in the configured tables — library(action='repair', dry_run=false)"
    if t == "lib_footprint_mismatch":
        return "board footprint differs from the library copy — fine if intentional (KiCad keeps the board copy)"
    if t in ("drill_out_of_range", "via_diameter", "annular_width", "microvia_drill_out_of_range", "hole_to_hole", "holes_co_located"):
        return (f"use {fab['who']}-capable holes: via ≥ {fab['via'][0]:g}/{fab['via'][1]:g} mm, PTH ring ≥ {fab['pth_annular']:g} mm, "
                f"holes ≥ {fab['hole_to_hole']:g} mm apart — pcb_setup(rules='{fab['preset']}') then fix the offending vias/pads")
    if t in ("track_width", "connection_width"):
        return (f"widen the track(s) on {', '.join(nets) or 'that net'} to ≥ the class minimum "
                f"({fab['who']} {fab['min_track']:g} mm)")
    if t == "invalid_outline":
        return "draw one closed Edge.Cuts outline: pcb_setup(width=…, height=…) (or polygon=[[x,y],…])"
    if t in ("zones_intersect", "isolated_copper", "starved_thermal", "zone_has_empty_net", "copper_sliver"):
        return "adjust the copper pour: pcb_pour(net=…, clearance=…, thermal_spoke=…) or pcb_pour(fill_only=true) to refill"
    if t in ("via_dangling", "track_dangling"):
        tr = list(dict.fromkeys(i["uuid"] for i in items if i.get("uuid") and i.get("kind") in _COPPER_KINDS))
        return ("delete the dangling via/track stub (it does nothing and can act as an antenna)"
                + (f": pcb_edit(ops=[{{\"op\":\"delete\",\"uuids\":{json.dumps(tr)}}}])" if tr else ""))
    if t in ("silk_overlap", "silk_over_copper", "silk_edge_clearance", "silk_clearance"):
        return (f"cosmetic: {fab['who'] if fab['who'] != 'generic' else 'the fab'} clips silkscreen off pads — usually waivable; "
                f"move refdes text if it becomes unreadable")
    if t in ("text_height", "text_thickness"):
        return (f"make silkscreen text ≥ 1.0 mm high and ≥ {fab['min_text_thickness']:g} mm thick so "
                f"{fab['who'] if fab['who'] != 'generic' else 'the fab'} can print it")
    if t == "solder_mask_bridge":
        return "pads of different nets share one mask opening — increase spacing or allow mask bridges for that footprint"
    if t == "item_on_disabled_layer":
        return "an item is on a layer the board doesn't have — move it or enable the layer"
    if t == "padstack":
        return "pad definition is invalid for fabrication (e.g. hole larger than pad) — fix the footprint"
    if t == "missing_courtyard" or t == "malformed_courtyard":
        return "footprint has no/invalid courtyard — fix or replace the footprint (parts_import gives clean ones)"
    return f"inspect it: render(target='pcb', crop=[x-5,y-5,x+5,y+5]) around 'at', then fix {r}"


# ── running kicad-cli ─────────────────────────────────────────────────────
def _run_json(args: list[str], out: Path, what: str, timeout: int = 600) -> dict:
    if out.exists():
        out.unlink()
    r = cli(args, timeout=timeout)
    if not out.exists() or out.stat().st_size == 0:
        msg = (r.stderr or r.stdout or "").strip()[-900:]
        raise ToolError(f"kicad-cli {what} failed (exit {r.returncode}): {msg or 'no output'}", code=f"{what.upper()}_FAILED",
                        fix="open the file in KiCad to see the load error, or checkpoint(action='list') to restore a good revision")
    try:
        return json.loads(out.read_text(encoding="utf-8", errors="replace"))
    except ValueError as e:
        raise ToolError(f"could not parse kicad-cli {what} JSON: {e}", code=f"{what.upper()}_FAILED")


_UUID_RE = re.compile(r'\(uuid "?([0-9a-fA-F-]{36})"?\)')


def _uuid_sheets(pr) -> dict:
    """item uuid → (sheet file name, sheet name of its first instance; '' for the root)."""
    out: dict = {}
    try:
        files = pr.sheets()
    except OSError:
        return out
    names: dict = {}
    for f in files:
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'\(property "Sheetname" "([^"]*)"[\s\S]{0,600}?\(property "Sheetfile" "([^"]*)"', txt):
            names.setdefault(Path(m.group(2)).name.lower(), m.group(1))
        for u in _UUID_RE.findall(txt):
            out.setdefault(u, (f.name, None))
    root = pr.sch.name.lower()
    return {u: (fn, "" if fn.lower() == root else names.get(fn.lower(), Path(fn).stem)) for u, (fn, _) in out.items()}


def record_external(pr, key: str, d: dict, source: str = ""):
    """Let pcb_* / sch_* tools that ran their own ERC/DRC update what verify/project_summary report as the latest
    result (counts + revision), without touching verify's finding signatures."""
    if key not in ("erc", "drc"):
        return
    prev = load_state(pr).get(key) or {}
    keep = {k: prev[k] for k in ("signatures", "messages", "top") if k in prev}
    rev = revision(pr.sch if key == "erc" else pr.pcb)
    save_state(pr, key, {**keep, "ts": now(), "revision": rev, "source": source or None,
                         **{k: d.get(k) for k in ("errors", "warnings", "unconnected", "parity") if d.get(k) is not None}})


def _symbol_positions(pr) -> dict:
    pos = {}
    for f in pr.sheets():
        try:
            tree = sexpr.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for s in sexpr.find_all(tree, "symbol"):
            at = sexpr.find(s, "at")
            u = sexpr.value(s, "uuid")
            if at is not None and u and len(at) >= 3:
                try:
                    pos[str(u)] = (float(at[1]), float(at[2]))
                except ValueError:
                    pass
    return pos


def _erc_scale(data: dict, pr) -> float:
    """KiCad up to 10.0.6 writes ERC JSON positions 100× too small (schematic
    IU mix-up, KiCad issue 25582, fixed in 10.0.7) while still claiming 'mm'.

    Measured, not assumed: the ratio between a symbol's real position and the
    position the report gives for the same UUID decides on every version. The
    version the report names is only the tie-breaker when no violation item
    is a symbol."""
    unit = data.get("coordinate_units", "mm")
    base = {"mm": 1.0, "mils": 0.0254, "in": 25.4}.get(unit, 1.0)
    items = [i for s in data.get("sheets", []) for v in s.get("violations", []) for i in v.get("items", [])]
    if not items:
        return base
    try:
        sym = _symbol_positions(pr)
    except Exception:  # noqa: BLE001
        sym = {}
    ratios = []
    for i in items:
        p = sym.get(i.get("uuid"))
        ip = i.get("pos") or {}
        if not p:
            continue
        for axis, real in (("x", p[0]), ("y", p[1])):
            got = ip.get(axis)
            if got and abs(got) > 1e-9 and abs(real) > 1:
                ratios.append(real / (got * base))
                break
    if ratios:
        ratios.sort()
        r = ratios[len(ratios) // 2]
        if 50 < r < 200:
            return base * 100.0
        if 0.5 < r < 2:
            return base
    from ..kicad import compat
    v = compat.parse_version(data.get("kicad_version"))
    return base * (100.0 if v is None or v.t < (10, 0, 7) else 1.0)


def _delta(pr, key: str, findings: list[dict], counts: dict) -> dict:
    prev = load_state(pr).get(key) or {}
    prev_sigs = set(prev.get("signatures", []))
    cur_sigs = [f["_sig"] for f in findings]
    cur_set = set(cur_sigs)
    first = not prev
    new = [f for f in findings if f["_sig"] not in prev_sigs]
    fixed_sigs = [s for s in prev_sigs if s not in cur_set]
    # what got fixed, as readable messages ("Pin not connected — U1.3"), grouped when repeated
    prev_msgs = prev.get("messages") or {}
    groups: dict = {}
    for sg in fixed_sigs:
        msg = prev_msgs.get(sg) or sg.split("|")[1]
        groups.setdefault(msg.split(" — ")[0], []).append(msg)
    fixed_desc = [(v[0] if len(v) == 1 else f"{k} ×{len(v)}") for k, v in groups.items()]
    save_state(pr, key, {"ts": now(), "revision": counts.get("revision"), "signatures": cur_sigs,
                         "messages": {f["_sig"]: f["message"][:160] for f in findings[:3000]},
                         **{k: v for k, v in counts.items() if k != "revision"},
                         "top": [{k: f[k] for k in ("severity", "type", "message", "fix") if k in f} for f in findings[:30]]})
    return {"first_run": first, "new": len(new), "fixed": len(fixed_sigs),
            "new_findings": [_public(f) for f in new[:15]], "fixed_findings": fixed_desc[:15],
            "previous": {k: prev.get(k) for k in ("ts", "errors", "warnings", "unconnected") if k in prev} or None}


def _public(f: dict) -> dict:
    return {k: v for k, v in f.items() if not k.startswith("_")}


# ── ERC ───────────────────────────────────────────────────────────────────
def run_erc(pr) -> dict:
    if not pr.sch.exists():
        raise ToolError(f"no schematic at {pr.sch}", code="NO_SCHEMATIC", fix="project_create(...) or project_open(path=...)")
    out = work_dir(pr) / "erc.json"
    data = _run_json(["sch", "erc", "--format", "json", "--severity-all", "--units", "mm", "-o", str(out), str(pr.sch)],
                     out, "erc")
    scale = _erc_scale(data, pr)
    ctx = _Ctx(pr)
    owner = _uuid_sheets(pr)
    findings = []
    for sh in data.get("sheets", []):
        kicad_sheet = sh.get("path", "/")
        for v in sh.get("violations", []):
            t = v.get("type", "unknown")
            sev = v.get("severity", "warning")
            items = [parse_item(i.get("description", "")) for i in v.get("items", [])]
            for it, raw in zip(items, v.get("items", [])):
                p = raw.get("pos") or {}
                if "x" in p:
                    it["at"] = [round(p["x"] * scale, 3), round(p.get("y", 0) * scale, 3)]
                if raw.get("uuid"):
                    it["uuid"] = raw["uuid"]
            at = items[0].get("at") if items else None
            # KiCad files some sub-sheet items (wire ends, labels) under the root path: trust the item's own file
            sheet, sheet_file = kicad_sheet, None
            hit = next((owner[i["uuid"]] for i in items if i.get("uuid") in owner), None)
            if hit:
                sheet_file = hit[0]
                if kicad_sheet.strip("/") == "" and hit[1]:
                    sheet = f"/{hit[1]}/"
            msg = v.get("description", t)
            who = [i.get("pin_ref") or i.get("ref") or (f"label {i['net']}" if i.get("net") else None) for i in items]
            who = [w for w in who if w]
            if who:
                msg += " — " + ", ".join(dict.fromkeys(who))
            extra = {}
            if t in ("power_pin_not_driven", "pin_not_driven", "ground_pin_not_ground"):
                nets = [ctx.net_of(i["pin_ref"]) for i in items if i.get("pin_ref")]
                nets = [n for n in nets if n]
                if nets:
                    extra["net"] = nets[0]
                    msg += f" (net {nets[0]})"
            if t in ESCALATE_ERC and sev != "exclusion":
                if sev != "error":
                    extra["escalated_from"] = sev
                sev = "error"
                msg += f" [escalated: {ESCALATE_ERC[t]}]"
            fix = _erc_fix(t, items, v.get("description", ""), ctx)
            fix = fix.replace("{SHEETFILE}", sheet_file or pr.sch.name)
            if at:
                x, y = at
                fix = fix.replace("crop=[x-15,y-15,x+15,y+15]) around 'at'",
                                  f"crop=[{x - 15:g},{y - 15:g},{x + 15:g},{y + 15:g}])")
            sname = sheet.strip("/").split("/")[-1] if sheet.strip("/") else ""
            if sname:
                # one sch_apply call edits one sheet: route the fix to the sheet the problem is on
                fix = fix.replace("sch_apply(ops=", f"sch_apply(sheet={_q(sname)}, ops=")
                fix = fix.replace("render(target='schematic', crop=", f"render(target='schematic', sheet={_q(sname)}, crop=")
            f = finding(sev, t, msg, fix, items, at, sheet=sheet, sheet_file=sheet_file, **extra)
            f["_sig"] = kicad_sheet + "|" + _sig(v)
            findings.append(f)
    findings = sort_findings(findings)
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    excluded = sum(1 for f in findings if f["severity"] == "exclusion")
    by_type: dict = {}
    for f in findings:
        if f["severity"] != "exclusion":
            by_type[f["type"]] = by_type.get(f["type"], 0) + 1
    counts = {"errors": errors, "warnings": warnings, "excluded": excluded, "revision": revision(pr.sch)}
    delta = _delta(pr, "erc", [f for f in findings if f["severity"] != "exclusion"], counts)
    return {"errors": errors, "warnings": warnings, "excluded": excluded, "by_type": by_type,
            "violations": [_public(f) for f in findings if f["severity"] != "exclusion"],
            "report": str(out), "kicad_version": data.get("kicad_version"),
            "coordinate_scale_fixed": scale != 1.0, **delta}


def erc_brief(pr, limit: int = 12) -> dict:
    r = run_erc(pr)
    top = [{k: f.get(k) for k in ("severity", "type", "message", "at", "fix")} for f in r["violations"][:limit]]
    return {"errors": r["errors"], "warnings": r["warnings"], "new": r["new"], "fixed": r["fixed"],
            "by_type": r["by_type"], "top": top, "report": r["report"],
            "truncated": max(0, len(r["violations"]) - limit)}


# ── footprint-owned Edge.Cuts ─────────────────────────────────────────────
# KiCad counts Edge.Cuts graphics INSIDE a footprint as board outline. Converted connector footprints (USB-C plugs,
# card-edge parts) often carry a 'board edge goes here' line there: once placed it is a stray open segment in the
# outline. KiCad then reports only a generic invalid_outline; this names the footprint and the way out.
_EDGE_LAYER_RE = re.compile(r'\(layer\s+"?Edge\.Cuts"?\s*\)')
_FP_REF_RE = re.compile(r'\(property\s+"Reference"\s+"((?:[^"\\]|\\.)*)"|\(fp_text\s+reference\s+"?([^"\s)]+)')
_FP_AT_RE = re.compile(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(?:\s+(-?[\d.]+))?\s*\)')
EDGE_DOC_LAYER = "Dwgs.User"


def _close(text: str, i: int) -> int:
    """Index just past the ')' closing the '(' at text[i] (strings respected)."""
    depth, n, in_s = 0, len(text), False
    while i < n:
        c = text[i]
        if in_s:
            if c == "\\":
                i += 1
            elif c == '"':
                in_s = False
        elif c == '"':
            in_s = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _fp_edge_spans(text: str) -> list[tuple[int, int, int, int]]:
    """Every footprint graphic on Edge.Cuts in board text → (footprint start, graphic start, graphic end, layer pos)."""
    out = []
    for m in _EDGE_LAYER_RE.finditer(text):
        g = text.rfind("(fp_", 0, m.start())
        if g < 0:
            continue
        end = _close(text, g)
        if end <= m.start():
            continue                             # a board-level graphic that merely follows a footprint
        f = max(text.rfind("(footprint ", 0, g), text.rfind("(module ", 0, g))
        if f >= 0:
            out.append((f, g, end, m.start()))
    return out


def _fp_head(text: str, f: int) -> tuple[str, float, float, float]:
    """(reference, x, y, rotation) of the footprint starting at text[f]."""
    nxt = min((i for i in (text.find("(footprint ", f + 1), text.find("(module ", f + 1)) if i > 0), default=len(text))
    m = _FP_REF_RE.search(text, f, nxt)
    ref = (m.group(1) or m.group(2) or "") if m else ""
    a = _FP_AT_RE.search(text, f, nxt)
    if not a:
        return ref, 0.0, 0.0, 0.0
    return ref, float(a.group(1)), float(a.group(2)), float(a.group(3) or 0)


def _xy_pairs(node):
    if not isinstance(node, list) or not node:
        return
    if node[0] in ("xy", "start", "end", "mid", "center", "at") and len(node) >= 3:
        try:
            yield float(node[1]), float(node[2])
        except (TypeError, ValueError):
            pass
        return
    for e in node[1:]:
        yield from _xy_pairs(e)


def footprint_edge_cuts(pcb_text: str) -> list[dict]:
    """Footprints that own graphics on Edge.Cuts → [{'ref','at','count','kinds','closed','bbox'}] (board mm)."""
    by_fp: dict = {}
    for f, g, end, _ in _fp_edge_spans(pcb_text):
        by_fp.setdefault(f, []).append(pcb_text[g:end])
    res = []
    for f, blocks in by_fp.items():
        ref, ox, oy, rot = _fp_head(pcb_text, f)
        cs, sn = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        pts, ends, kinds, closed = [], {}, [], True
        for b in blocks:
            try:
                t = sexpr.loads(b)
                kind = str(t[0])
                loc = list(_xy_pairs(t))
            except Exception:  # noqa: BLE001 — an unreadable graphic is still reported, just without geometry
                closed = False
                continue
            kinds.append(kind)
            if kind == "fp_circle" and len(loc) >= 2:
                r = math.hypot(loc[1][0] - loc[0][0], loc[1][1] - loc[0][1])
                loc = [(loc[0][0] - r, loc[0][1] - r), (loc[0][0] + r, loc[0][1] + r)]
            elif kind in ("fp_line", "fp_arc"):
                for nm in ("start", "end"):
                    e = sexpr.find(t, nm)
                    if e is not None:
                        k = (round(float(e[1]), 3), round(float(e[2]), 3))
                        ends[k] = ends.get(k, 0) + 1
            elif kind not in ("fp_rect", "fp_poly"):
                closed = False                   # text, curves: never a clean cut-out
            pts += [(ox + x * cs + y * sn, oy - x * sn + y * cs) for x, y in loc]
        closed = closed and all(v % 2 == 0 for v in ends.values())
        bbox = None
        if pts:
            bbox = [round(min(p[0] for p in pts), 3), round(min(p[1] for p in pts), 3),
                    round(max(p[0] for p in pts), 3), round(max(p[1] for p in pts), 3)]
        res.append({"ref": ref, "at": [ox, oy], "count": len(blocks), "kinds": sorted(set(kinds)), "closed": closed,
                    "bbox": bbox})
    return sorted(res, key=lambda d: d["ref"])


def move_footprint_edge_cuts(pr, refs: list[str] | None = None, to_layer: str = EDGE_DOC_LAYER) -> dict:
    """Re-layer footprint-owned Edge.Cuts graphics in the saved board (all footprints, or only `refs`).
    A text edit that touches nothing but those `(layer …)` tokens. → {'moved': {ref: n}, 'to_layer'}."""
    with open(pr.pcb, encoding="utf-8", newline="") as fh:
        text = fh.read()
    want = set(refs) if refs else None
    moved: dict = {}
    out, last = [], 0
    for f, _g, _end, lp in _fp_edge_spans(text):
        ref = _fp_head(text, f)[0]
        if want is not None and ref not in want:
            continue
        out += [text[last:lp], f'(layer "{to_layer}")']
        last = _EDGE_LAYER_RE.match(text, lp).end()
        moved[ref] = moved.get(ref, 0) + 1
    if moved:
        with open(pr.pcb, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(out) + text[last:])
    return {"moved": moved, "to_layer": to_layer}


def _fp_edge_findings(pr) -> list[dict]:
    try:
        text = pr.pcb.read_text(encoding="utf-8", errors="replace")
        if "Edge.Cuts" not in text:
            return []
        hits = footprint_edge_cuts(text)
    except Exception:  # noqa: BLE001 — an extra check must never fail a DRC run
        return []
    out = []
    for h in hits:
        ref = h["ref"] or "?"
        n = h["count"]
        what = f"{n} graphic{'s' if n != 1 else ''} ({', '.join(k[3:] for k in h['kinds']) or 'unreadable'})"
        if h["closed"]:
            sev = "warning"
            msg = (f"Footprint {ref} owns {what} on Edge.Cuts — KiCad cuts this closed shape out of the board; "
                   f"keep it only if the part really needs that cut-out")
        else:
            sev = "error"
            msg = (f"Footprint {ref} owns {what} on Edge.Cuts that do not form a closed shape — KiCad treats them as "
                   f"board outline, so they corrupt it (invalid outline, wrong board size, edge-clearance errors)")
        bb = h["bbox"]
        where = ""
        if bb:
            msg += f"; they span [{bb[0]:g},{bb[1]:g}]–[{bb[2]:g},{bb[3]:g}] mm on the board"
            if not h["closed"]:
                if bb[3] - bb[1] < 0.01 <= bb[2] - bb[0]:
                    where = f" The line marks where the board edge belongs: y = {bb[1]:g} mm."
                elif bb[2] - bb[0] < 0.01 <= bb[3] - bb[1]:
                    where = f" The line marks where the board edge belongs: x = {bb[0]:g} mm."
                else:
                    where = " Such lines usually mark where the board edge (or a cut-out) belongs."
        fix = (f"move them to a documentation layer: in {pr.pcb.name}, inside footprint {ref}, change (layer \"Edge.Cuts\") to "
               f"(layer \"{EDGE_DOC_LAYER}\") on its {n} fp_* graphic{'s' if n != 1 else ''} (direct text edit with the board closed "
               f"in KiCad), then verify(what=['drc']).{where} Draw the real edge with pcb_setup(width=…, height=…) or "
               f"polygon=[[x,y],…]. If the footprint came from parts_import, re-import the part so the library copy is "
               f"clean too (the importer moves Edge.Cuts graphics to {EDGE_DOC_LAYER})")
        f = finding(sev, "footprint_edge_cuts", msg, fix, [{"ref": ref, "kind": "footprint", "at": h["at"]}], h["at"],
                    section="violations", closed=h["closed"], bbox=bb)
        f["_sig"] = f"violations|footprint_edge_cuts|{ref}|{n}"
        out.append(f)
    return out


# ── DRC ───────────────────────────────────────────────────────────────────
def run_drc(pr, refill: bool = True, parity: bool = True) -> dict:
    if not pr.pcb.exists():
        raise ToolError(f"no board at {pr.pcb}", code="NO_PCB",
                        fix="pcb_sync() creates/updates the board from the schematic")
    out = work_dir(pr) / "drc.json"
    args = ["pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "-o", str(out)]
    if refill:
        args.append("--refill-zones")       # refills in memory only — the file is never saved
    if parity and pr.sch.exists():
        args.append("--schematic-parity")
    data = _run_json(args + [str(pr.pcb)], out, "drc")
    fab_hint = _fab_rules(pr)               # hint text quotes the project's fab profile, not a fixed vendor
    unit = data.get("coordinate_units", "mm")
    scale = {"mm": 1.0, "mils": 0.0254, "in": 25.4}.get(unit, 1.0)
    findings = []
    for section in ("violations", "unconnected_items", "schematic_parity"):
        for v in data.get(section, []) or []:
            t = v.get("type", section)
            sev = v.get("severity", "error")
            items = [parse_item(i.get("description", "")) for i in v.get("items", [])]
            for it, raw in zip(items, v.get("items", [])):
                p = raw.get("pos") or {}
                if "x" in p:
                    it["at"] = [round(p["x"] * scale, 4), round(p.get("y", 0) * scale, 4)]
                if raw.get("uuid"):
                    it["uuid"] = raw["uuid"]     # `kind` (track / via / pad / zone …) comes from parse_item
            at = items[0].get("at") if items else None
            msg = v.get("description", t)
            who = [i.get("pin_ref") or i.get("ref") for i in items]
            who = [w for w in who if w]
            if who:
                msg += " — " + ", ".join(dict.fromkeys(who))
            fx = _drc_fix(t, items, v.get("description", ""), fab_hint)
            if at:
                fx = fx.replace("crop=[x-5,y-5,x+5,y+5]) around 'at'", f"crop=[{at[0] - 5:g},{at[1] - 5:g},{at[0] + 5:g},{at[1] + 5:g}])")
            f = finding(sev, t, msg, fx, items, at, section=section)
            f["_sig"] = section + "|" + _sig(v)
            findings.append(f)
    edge = _fp_edge_findings(pr)                # KiCad itself only says 'invalid outline': name the footprint
    if edge:
        culprits = ", ".join(dict.fromkeys(f["items"][0]["ref"] for f in edge if not f.get("closed")))
        for f in findings:
            if f["type"] == "invalid_outline" and culprits:
                f["fix"] = (f"footprint {culprits} owns Edge.Cuts graphics — fix the footprint_edge_cuts finding first; "
                            f"if the outline is still invalid after that: {f['fix']}")
        findings += edge
    findings = sort_findings(findings)
    live = [f for f in findings if f["severity"] != "exclusion"]
    errors = sum(1 for f in live if f["severity"] == "error" and f.get("section") == "violations")
    warnings = sum(1 for f in live if f["severity"] == "warning")
    unconnected = sum(1 for f in live if f.get("section") == "unconnected_items")
    par = sum(1 for f in live if f.get("section") == "schematic_parity")
    by_type: dict = {}
    for f in live:
        by_type[f["type"]] = by_type.get(f["type"], 0) + 1
    counts = {"errors": errors, "warnings": warnings, "unconnected": unconnected, "parity": par,
              "excluded": len(findings) - len(live), "revision": revision(pr.pcb)}
    delta = _delta(pr, "drc", live, counts)
    unrouted_nets = sorted({i["net"] for f in live if f.get("section") == "unconnected_items"
                            for i in f.get("items", []) if i.get("net")})
    return {"errors": errors, "warnings": warnings, "unconnected": unconnected, "parity": par,
            "excluded": counts["excluded"], "by_type": by_type, "unrouted_nets": unrouted_nets,
            "violations": [_public(f) for f in live], "report": str(out),
            "refilled_zones": refill, "parity_checked": bool(parity and pr.sch.exists()),
            "kicad_version": data.get("kicad_version"), **delta}


def last_results(pr) -> dict:
    """What the previous verify stored (no KiCad call)."""
    st = load_state(pr)
    out = {}
    for k in ("erc", "drc"):
        if k in st:
            s = st[k]
            out[k] = {kk: s.get(kk) for kk in ("ts", "errors", "warnings", "unconnected", "parity", "revision", "source")
                      if s.get(kk) is not None}
            cur = revision(pr.sch if k == "erc" else pr.pcb)
            out[k]["stale"] = bool(s.get("revision") and cur and s.get("revision") != cur)
    return out
