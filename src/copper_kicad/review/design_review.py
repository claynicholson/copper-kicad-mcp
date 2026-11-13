"""Engineering design review over the schematic netlist.

These are the mistakes that pass ERC but still produce a dead board:
missing decoupling, I2C without pull-ups, crystals without load caps, USB-C
sinks without 5.1 k CC pull-downs, unprotected USB data lines, regulators
without input/output caps, floating enable pins, LEDs without current
limiting, unsourced parts, single-pin nets and near-duplicate net names.

Every heuristic is written to stay quiet unless it is fairly sure: when in
doubt it reports `info`, not `warning`.
"""
from __future__ import annotations

import re

import fnmatch
import json
import math

from .. import sexpr
from .common import (base_net, comp_kind, field_ci, finding, is_auto_net, is_ground_net, is_ground_pin, is_load_part,
                     is_power_net, is_supply_pin, lcsc_of, load_netlist, natural_key, parse_value, ref_prefix, sort_findings)

CHECKS = ["decoupling", "i2c_pullups", "crystal_caps", "usb_c_cc", "usb_esd", "regulator_caps", "enable_pins",
          "led_resistors", "power_flags", "sourcing", "footprints", "single_pin_nets", "net_names",
          "regulator_setpoint", "power_path", "thermal_pad", "schematic_style"]
# the drawing-style pass (review/sch_style.py) runs only when asked for: checks=['schematic_style']
DEFAULT_CHECKS = [c for c in CHECKS if c != "schematic_style"]

JLC_EXTENDED_FEE_USD = 3.0     # JLC charges a feeder-loading fee per unique Extended part (standard PCBA)


def _clean_pin(name: str) -> list[str]:
    """'~{RESET}/PA0' → ['RESET','PA0'];  'nRST' stays (handled by regex)."""
    n = re.sub(r"~\{([^}]*)\}", r"\1", name or "").replace("~", "")
    return [p.strip().upper() for p in re.split(r"[/,]", n) if p.strip()]


class _Model:
    def __init__(self, nl: dict):
        self.comps: dict = nl["components"]
        self.nets: dict = nl["nets"]
        self.pin_net: dict = {}
        self.comp_pins: dict = {}
        for name, nodes in self.nets.items():
            for n in nodes:
                self.pin_net[(n["ref"], n["pin"])] = name
                self.comp_pins.setdefault(n["ref"], []).append({**n, "net": name})
        self.kind = {r: comp_kind(r, c) for r, c in self.comps.items()}
        # ground by name, plus nets an IC uses as its ground reference under another name (DW01A GND on BATN)
        # parts drawn like an IC that have nothing to decouple: heaters, motors, coils, bare sensing elements.
        # Net roles are judged without their pins: a heater pin typed 'power input' makes no rail.
        self.loads = {r for r, k in self.kind.items()
                      if k in ("ic", "other") and is_load_part(self.comps[r], self.comp_pins.get(r))}
        live = {n: ([x for x in nodes if x["ref"] not in self.loads] if self.loads else nodes)
                for n, nodes in self.nets.items()}
        self.gnd = {n for n, nodes in live.items() if is_ground_net(n, nodes)}
        self.power = {n for n, nodes in live.items() if is_power_net(n, nodes)}
        self.pr = None              # set by review(): board-aware checks read pr.pcb / pr.pro
        self._board = None

    def ic_grounds(self, ref: str) -> set:
        """The nets this part's own GND/VSS pins sit on (its decoupling reference)."""
        return {p["net"] for p in self.comp_pins.get(ref, [])
                if is_ground_pin(p) and not p["net"].startswith("unconnected-")}

    def board(self) -> dict | None:
        if self._board is None:
            try:
                self._board = _read_board(self.pr) if self.pr is not None else {}
            except Exception:  # noqa: BLE001 — board-aware checks are best effort
                self._board = {}
        return self._board or None

    def two_pin_between(self, kind: str, a: str, b_set: set) -> list[str]:
        """Refs of 2-pin `kind` components with one pin on net a and the other on any of b_set."""
        out = []
        for ref, pins in self.comp_pins.items():
            if self.kind.get(ref) != kind or len(pins) != 2 or self.comps.get(ref, {}).get("dnp"):
                continue        # a DNP part is not on the assembled board
            nets = {p["net"] for p in pins}
            if a in nets and (nets - {a}) & b_set:
                out.append(ref)
        return out

    def comps_on(self, net: str, kind: str | None = None) -> list[str]:
        refs = sorted({n["ref"] for n in self.nets.get(net, [])}, key=natural_key)
        return [r for r in refs if kind is None or self.kind.get(r) == kind]

    def text(self, ref: str) -> str:
        c = self.comps.get(ref, {})
        return f"{c.get('lib_id', '')} {c.get('value', '')} {c.get('description', '')} {c.get('footprint', '')}".lower()


# ── individual checks ─────────────────────────────────────────────────────
def _ic_power_rails(m: _Model, ref: str) -> dict:
    """{rail: [pins]} of an IC's supply pins. A pin counts by electrical type or canonical name (is_supply_pin:
    never a ground or a heater/coil terminal), the part must have electronics (not in m.loads), and the net
    must be a supply rail (m.power) that is not the part's own ground reference."""
    rails: dict = {}
    if ref in m.loads:
        return rails
    own_gnd = m.ic_grounds(ref)
    for p in m.comp_pins.get(ref, []):
        if (is_supply_pin(p) and p["net"] not in m.gnd and p["net"] not in own_gnd and not p["net"].startswith("unconnected-")
                and p["net"] in m.power):
            rails.setdefault(p["net"], []).append(p["pin"])
    return rails


def check_decoupling(m: _Model) -> list[dict]:
    out = []
    rail_ics: dict = {}
    for ref in sorted(m.comps, key=natural_key):
        if m.kind.get(ref) != "ic" or m.comps[ref].get("dnp"):
            continue
        if len(m.comp_pins.get(ref, [])) < 3:
            continue
        for rail, pins in _ic_power_rails(m, ref).items():
            rail_ics.setdefault(rail, []).append((ref, pins))
    if not m.gnd:
        return out
    for rail, ics in rail_ics.items():
        caps = m.two_pin_between("capacitor", rail, m.gnd)
        small = [c for c in caps if (parse_value(m.comps[c].get("value", "")) or 0) <= 2.2e-6]
        n_pins = sum(len(p) for _, p in ics)
        refs = [r for r, _ in ics]
        lacking = []
        for ref, pins in ics:
            # measured against the IC's OWN ground pin net: a protection IC's GND is cell-negative (BATN), not GND
            own = m.ic_grounds(ref)
            if not own or all(is_ground_net(n) for n in own):
                own = m.gnd                 # an ordinary GND/AGND/… IC: a cap to any named ground counts
            if not m.two_pin_between("capacitor", rail, own):
                lacking.append((ref, pins, own))
        if lacking:
            for ref, pins, own in lacking:
                g = sorted(own, key=lambda n: (base_net(n).upper() != "GND", n))[0]
                out.append(finding(
                    "warning", "missing_decoupling",
                    f"{ref} power pin(s) {', '.join(pins)} on {rail} have no decoupling capacitor (none between {rail} and "
                    f"{ref}'s ground {g})",
                    fix=(f"sch_apply(ops=[{{\"op\":\"place\",\"spec\":\"100nF 0402\",\"ref\":\"C?\",\"near\":\"{ref}\"}},"
                         f"{{\"op\":\"connect\",\"pin\":\"C?.1\",\"net\":\"{base_net(rail)}\"}},{{\"op\":\"connect\",\"pin\":\"C?.2\",\"net\":\"{base_net(g)}\"}}]) "
                         f"— one 100 nF per power pin plus one 10 µF bulk per rail"),
                    items=[f"{ref}.{p}" for p in pins], net=rail))
        elif not caps:
            continue
        elif len(small) < len(refs) and n_pins > 1:
            out.append(finding(
                "info", "thin_decoupling",
                f"{rail} feeds {n_pins} IC power pin(s) on {', '.join(refs)} but has {len(small)} small (≤2.2 µF) decoupling cap(s): {', '.join(small) or 'none'}",
                fix=f"add a 100 nF (spec '100nF 0402') per IC power pin on net {base_net(rail)}, placed next to the pin",
                items=refs, net=rail))
    return out


_I2C_RE = re.compile(r"(?:^|[_\W])(SDA|SCL)\d*(?:$|[_\W])|^I2C\d*_?(SDA|SCL)|^(SDA|SCL)\d*$", re.I)


def check_i2c_pullups(m: _Model) -> list[dict]:
    out = []
    for net, nodes in m.nets.items():
        b = base_net(net)
        is_i2c = bool(_I2C_RE.search(b)) and not is_auto_net(net)
        if not is_i2c:
            # unnamed net: look at the pin names on it
            names = {x for n in nodes for x in _clean_pin(n.get("pin_name", ""))}
            is_i2c = bool(names & {"SDA", "SCL", "I2C_SDA", "I2C_SCL"}) and \
                sum(1 for n in nodes if m.kind.get(n["ref"]) == "ic") >= 2
        if not is_i2c or len(nodes) < 2:
            continue
        pullups = m.two_pin_between("resistor", net, m.power)
        if pullups:
            continue
        # a resistor pack or an IC with an explicit pull-up pin also counts — be conservative
        if any(m.kind.get(n["ref"]) == "resistor" for n in nodes):
            continue
        sig = "SCL" if "SCL" in b.upper() else "SDA"
        out.append(finding(
            "warning", "i2c_no_pullup",
            f"I2C net {net} ({sig}) has no pull-up resistor to a supply rail — the bus will not idle high",
            fix=(f"sch_apply(ops=[{{\"op\":\"place\",\"spec\":\"4.7k 0402\",\"ref\":\"R?\"}},"
                 f"{{\"op\":\"connect\",\"pin\":\"R?.1\",\"net\":\"{base_net(net)}\"}},{{\"op\":\"connect\",\"pin\":\"R?.2\",\"net\":\"+3V3\"}}]) "
                 f"(use the bus's I/O rail; 2.2k–10k)"),
            items=[f"{n['ref']}.{n['pin']}" for n in nodes][:6], net=net))
    return out


def check_crystal_caps(m: _Model) -> list[dict]:
    out = []
    for ref in sorted(m.comps, key=natural_key):
        if m.kind.get(ref) != "crystal" or m.comps[ref].get("dnp"):
            continue
        t = m.text(ref)
        if "resonator" in t and len(m.comp_pins.get(ref, [])) >= 3:
            continue  # ceramic resonators with built-in caps
        terms = [p for p in m.comp_pins.get(ref, []) if p["net"] not in m.gnd and not p["net"].startswith("unconnected-")]
        if len(terms) != 2:
            continue
        missing = [p for p in terms if not m.two_pin_between("capacitor", p["net"], m.gnd)]
        if not missing:
            continue
        # 32.768 kHz crystals on chips with internal load caps are common, and a cap in series with a
        # terminal means a non-Pierce topology (trimmer/series resonance) — report those as notes only
        series_cap = any(r != ref and m.kind.get(r) == "capacitor" for p in terms for r in m.comps_on(p["net"]))
        sev = "info" if ("32.768" in t or "32k" in t or series_cap) else "warning"
        out.append(finding(
            sev, "crystal_no_load_caps",
            f"crystal {ref} terminal(s) {', '.join(p['net'] for p in missing)} have no load capacitor to GND",
            fix=(f"add a load cap per terminal: CL_each ≈ 2×(CL − Cstray≈3pF), e.g. 2×(10pF−3pF)=14pF→ spec '15pF 0402' via "
                 f"sch_apply place+connect each to its terminal net and GND (skip only if the MCU datasheet specifies internal load caps)"),
            items=[f"{ref}.{p['pin']}" for p in missing]))
    return out


def _is_5k1(v: float | None) -> bool:
    return v is not None and 4.7e3 <= v <= 5.6e3


def check_usb_c_cc(m: _Model) -> list[dict]:
    out = []
    for ref in sorted(m.comps, key=natural_key):
        if m.kind.get(ref) != "connector":
            continue
        cc = [p for p in m.comp_pins.get(ref, []) if any(re.fullmatch(r"CC[12]?", n) for n in _clean_pin(p.get("pin_name", "")))]
        if not cc:
            continue
        nets = {}
        for p in cc:
            nets.setdefault(p["net"], []).append(p)
        if len(cc) >= 2 and len(nets) == 1 and not next(iter(nets)).startswith("unconnected-"):
            net = next(iter(nets))
            # tied together: only legal if driven by a controller
            if not [r for r in m.comps_on(net) if m.kind.get(r) == "ic"]:
                out.append(finding("error", "usb_c_cc_tied",
                                   f"{ref} CC1 and CC2 are tied to one net ({net}) — a USB-C source sees one 5.1k as the cable orientation; each CC needs its own 5.1 kΩ to GND",
                                   fix=f"give CC1 and CC2 separate nets, each with its own 5.1k (spec '5.1k 0402') to GND",
                                   items=[f"{ref}.{p['pin']}" for p in cc], net=net))
            continue
        for net, ps in nets.items():
            pin_ref = f"{ref}.{ps[0]['pin']}"
            if net.startswith("unconnected-") or len(m.nets.get(net, [])) <= 1:
                out.append(finding("error", "usb_c_cc_floating",
                                   f"{pin_ref} ({ps[0].get('pin_name') or 'CC'}) is unconnected — a USB-C charger will not supply VBUS to this sink",
                                   fix=(f"sch_apply(ops=[{{\"op\":\"place\",\"spec\":\"5.1k 0402\",\"ref\":\"R?\",\"near\":\"{ref}\"}},"
                                        f"{{\"op\":\"connect\",\"pin\":\"{pin_ref}\",\"net\":\"CC{ps[0].get('pin_name', '')[-1:] or ''}\"}},"
                                        f"{{\"op\":\"connect\",\"pin\":\"R?.1\",\"net\":\"CC{ps[0].get('pin_name', '')[-1:] or ''}\"}},{{\"op\":\"connect\",\"pin\":\"R?.2\",\"net\":\"GND\"}}])"),
                                   items=[pin_ref]))
                continue
            if [r for r in m.comps_on(net) if m.kind.get(r) == "ic"]:
                continue  # PD/CC controller handles it
            pulldowns = m.two_pin_between("resistor", net, m.gnd)
            pullups = m.two_pin_between("resistor", net, m.power)
            if pulldowns:
                bad = [r for r in pulldowns if not _is_5k1(parse_value(m.comps[r].get("value", "")))]
                if bad and len(bad) == len(pulldowns):
                    out.append(finding("warning", "usb_c_cc_wrong_value",
                                       f"{pin_ref} CC pull-down {', '.join(bad)} is {m.comps[bad[0]].get('value')} — a USB-C sink needs 5.1 kΩ (Rd)",
                                       fix=f"sch_apply(ops=[{{\"op\":\"set_field\",\"ref\":\"{bad[0]}\",\"field\":\"Value\",\"value\":\"5.1k\"}}]) and re-source it",
                                       items=[pin_ref, *bad], net=net))
            elif pullups:
                continue  # source/DFP role (Rp) — intentional
            else:
                out.append(finding("error", "usb_c_cc_no_rd",
                                   f"{pin_ref} (CC on net {net}) has no 5.1 kΩ pull-down to GND — chargers will not turn on VBUS",
                                   fix=(f"sch_apply(ops=[{{\"op\":\"place\",\"spec\":\"5.1k 0402\",\"ref\":\"R?\",\"near\":\"{ref}\"}},"
                                        f"{{\"op\":\"connect\",\"pin\":\"R?.1\",\"net\":\"{base_net(net)}\"}},{{\"op\":\"connect\",\"pin\":\"R?.2\",\"net\":\"GND\"}}])"),
                                   items=[pin_ref], net=net))
    return out


_ESD_RE = re.compile(r"esd|tvs|usblc|prtr|srv05|tpd\d|lesd|pesd|rclamp|esda|ip4220|dt104|sp05|cdsot|d3v3|tvs\d|\bsmf|smbj|smaj|pusb", re.I)


def check_usb_esd(m: _Model) -> list[dict]:
    out = []
    for ref in sorted(m.comps, key=natural_key):
        if m.kind.get(ref) != "connector":
            continue
        dpins = [p for p in m.comp_pins.get(ref, [])
                 if any(re.fullmatch(r"D[+-]\d?|DP\d?|DN\d?|D[PN]|USB_D[PN+-]", n) for n in _clean_pin(p.get("pin_name", "")))]
        if not dpins:
            continue
        nets = sorted({p["net"] for p in dpins if not p["net"].startswith("unconnected-")})
        if not nets:
            continue
        # only data lines that reach a transceiver need ESD: a charge-only port (D+ shorted to D-, or a resistor
        # divider signature) has no USB PHY behind it
        phys = sorted({r for n in nets for r in m.comps_on(n) if r != ref and m.kind.get(r) == "ic"
                       and not _ESD_RE.search(m.text(r) + " " + r.lower())}, key=natural_key)
        if not phys:
            continue
        protected = set()
        for net in nets:
            for r in m.comps_on(net):
                if r != ref and (m.kind.get(r) in ("diode",) or _ESD_RE.search(m.text(r))) and _ESD_RE.search(m.text(r) + " " + r.lower()):
                    protected.add(net)
        missing = [n for n in nets if n not in protected]
        if missing:
            out.append(finding("warning", "usb_no_esd",
                               f"USB data lines {', '.join(missing)} on {ref} have no ESD/TVS protection — cable discharges hit the USB PHY of {', '.join(phys[:3])}",
                               fix=("add a low-capacitance USB ESD array (e.g. USBLC6-2SC6, LCSC C7519) next to the connector: "
                                    "parts_search(query='USBLC6-2SC6') → sch_apply place + connect its I/O pins to D+/D-, GND and VBUS"),
                               items=[f"{ref}.{p['pin']}" for p in dpins], nets=missing))
    return out


_REG_RE = re.compile(r"regulator|\bldo\b|1117|me6211|xc6206|xc6220|ap2112|ap7333|ap2127|rt9013|rt9080|mcp1700|mcp1702|"
                     r"mcp1703|ht7333|ht7533|lm317|lm7805|78[0-9]{2}|tlv7[0-9]{2}|lp5907|ncp1117|spx3819|mic5219|sgm2019|tps7a|"
                     r"buck|boost|mp1584|mp2307|tps5|tps6|lm2596|sy8089|ap3417|xl1509|mt3608", re.I)


def check_regulator_caps(m: _Model) -> list[dict]:
    out = []
    for ref in sorted(m.comps, key=natural_key):
        if m.kind.get(ref) != "ic" or not _REG_RE.search(m.text(ref)) or m.comps[ref].get("dnp"):
            continue
        sides = {"input": [], "output": []}
        for p in m.comp_pins.get(ref, []):
            names = _clean_pin(p.get("pin_name", ""))
            if any(re.fullmatch(r"VIN\d?|IN|VI|VCC|PVIN|VBAT|VS", n) for n in names):
                sides["input"].append(p)
            elif any(re.fullmatch(r"VOUT\d?|OUT|VO|OUTPUT", n) for n in names):
                sides["output"].append(p)
        for side, pins in sides.items():
            nets = {p["net"] for p in pins if p["net"] not in m.gnd and not p["net"].startswith("unconnected-")}
            for net in nets:
                if not m.two_pin_between("capacitor", net, m.gnd):
                    rec = "1 µF–10 µF" if side == "input" else "the datasheet's minimum (typ. 1–22 µF, ceramic)"
                    out.append(finding("warning", f"regulator_no_{side}_cap",
                                       f"regulator {ref} ({m.comps[ref].get('value')}) {side} net {net} has no capacitor to GND — LDOs oscillate or droop without it",
                                       fix=(f"sch_apply(ops=[{{\"op\":\"place\",\"spec\":\"10uF 0805\",\"ref\":\"C?\",\"near\":\"{ref}\"}},"
                                            f"{{\"op\":\"connect\",\"pin\":\"C?.1\",\"net\":\"{base_net(net)}\"}},{{\"op\":\"connect\",\"pin\":\"C?.2\",\"net\":\"GND\"}}]) — {rec}"),
                                       items=[f"{ref}.{p['pin']}" for p in pins], net=net))
    return out


_EN_RE = re.compile(r"^(?:EN|ENABLE|CE|CHIP_EN|CHIP_PU|SHDN|~?SHDN|PWR_EN|ON|EN\d)$")
_RST_RE = re.compile(r"^(?:N?RST|N?RESET|RESET_?N|RST_?N|MCLR|XRES|RESETB|RSTB|NRST)$")
_BOOT_RE = re.compile(r"^(?:BOOT\d?|BOOTSEL|BOOT_?MODE|GPIO0|IO0|PROG|STRAP\w*)$")


def check_enable_pins(m: _Model) -> list[dict]:
    out = []
    for ref in sorted(m.comps, key=natural_key):
        if m.kind.get(ref) != "ic":
            continue
        for p in m.comp_pins.get(ref, []):
            names = _clean_pin(p.get("pin_name", ""))
            if not names:
                continue
            role = None
            if any(_EN_RE.match(n) for n in names):
                role = "enable"
            elif any(_RST_RE.match(n) for n in names):
                role = "reset"
            elif any(_BOOT_RE.match(n) for n in names) and len(names) == 1:
                role = "boot"
            if role is None or p.get("pin_type") in ("output", "power_out", "power_in"):
                continue
            net = p["net"]
            if len(m.nets.get(net, [])) > 1:
                continue
            pin_ref = f"{ref}.{p['pin']}"
            if role == "enable":
                out.append(finding("warning", "floating_enable",
                                   f"{pin_ref} ({p.get('pin_name')}) is not connected — the chip may never turn on (or turn on randomly)",
                                   fix=(f"tie it high through 10k (or straight to the input rail if always-on): "
                                        f"sch_apply(ops=[{{\"op\":\"connect\",\"pin\":\"{pin_ref}\",\"net\":\"<VIN rail>\"}}])"),
                                   items=[pin_ref]))
            else:
                out.append(finding("info", f"floating_{role}",
                                   f"{pin_ref} ({p.get('pin_name')}) is unconnected — fine only if the chip has an internal pull ({'many MCUs pull reset up internally' if role == 'reset' else 'boot strap default'})",
                                   fix=(f"recommended: 10k pull-up + 100nF to GND on reset, or a defined strap for boot pins; "
                                        f"sch_apply(ops=[{{\"op\":\"connect\",\"pin\":\"{pin_ref}\",\"net\":\"{'NRST' if role == 'reset' else 'BOOT'}\"}}, …])"),
                                   items=[pin_ref]))
    return out


def check_led_resistors(m: _Model) -> list[dict]:
    out = []
    for ref in sorted(m.comps, key=natural_key):
        if m.kind.get(ref) != "led":
            continue
        pins = m.comp_pins.get(ref, [])
        if len(pins) != 2:
            continue   # addressable / RGB / multi-die LEDs are out of scope
        t = m.text(ref)
        if re.search(r"ws28|sk68|apa10|neopixel|ir\b|laser", t):
            continue
        nets = [p["net"] for p in pins]
        if any(n.startswith("unconnected-") for n in nets):
            continue
        limited = False
        for n in nets:
            if n in m.gnd or (n in m.power and _is_rail(m, n)):
                continue        # a resistor elsewhere on a rail is not in series with this LED
            others = [r for r in m.comps_on(n) if r != ref]
            if any(m.kind.get(r) in ("resistor", "transistor", "inductor") for r in others):
                limited = True
            # LED driven by an IC that is a dedicated driver/current sink
            if any(m.kind.get(r) == "ic" and re.search(r"led|driver|current|tlc59|is31|pca96|ht16|max7219", m.text(r)) for r in others):
                limited = True
        if limited:
            continue
        rails = [n for n in nets if n in m.gnd or (n in m.power and _is_rail(m, n))]
        out.append(finding("warning", "led_no_resistor",
                           f"LED {ref} has no series resistor on either side ({' / '.join(nets)}) — it will over-current" +
                           (" straight across a rail" if len(rails) == 2 else ""),
                           fix=(f"insert a series resistor: R = (Vsupply − Vf) / I, e.g. (3.3−2.0)/2mA ≈ 680Ω → sch_apply disconnect "
                                f"{ref}.{pins[0]['pin']} then place spec '1k 0402' between it and net {base_net(nets[0])}"),
                           items=[f"{ref}.{p['pin']}" for p in pins]))
    return out


def _is_rail(m: _Model, net: str) -> bool:
    """A real supply rail (many loads / a source pin), not a 2-3 pin node that merely has a rail-like name."""
    nodes = m.nets.get(net, [])
    return len(nodes) > 3 or any(n.get("pin_type") in ("power_in", "power_out") for n in nodes)


def _sourcing_context(pr) -> dict:
    """Which field is 'the part number' for this project, whether a part needs one at all, and whether the
    fab profile has assembly price classes. Defaults (no project / unreadable config): LCSC + JLCPCB classes."""
    ctx = {"field": "LCSC", "label": "LCSC number", "required": True, "profile": None, "fee": JLC_EXTENDED_FEE_USD,
           "classes": True, "who": "JLC assembly"}
    from ..mcp.registry import ToolError
    try:
        from .. import config
        from ..parts import providers as PV
        from . import fab_profiles as FP
        cfg = config.load(pr)
        prof = FP.active(pr)
        provs = PV.active(cfg)
        field = (prof.get("bom") or {}).get("part_field") or PV.part_field(cfg, provs)
        pc = prof.get("part_classes") or {}
        ctx.update(field=field, profile=prof, classes=bool(pc.get("field")) and (field or "").upper() == "LCSC",
                   fee=float((pc.get("fee_usd") or {}).get("extended") or 0),
                   label=(prof.get("bom") or {}).get("part_label") or (f"{field} number" if field else "part number"),
                   who=f"{prof.get('short') or prof['label']} assembly",
                   # a generic provider (value + footprint is enough) makes numbers optional unless the fab insists
                   required=bool(field) and ("parts" in (prof.get("blocks") or []) or not PV.generic_ok(provs)))
    except ToolError as e:   # broken config / unknown provider or profile: the caller must not pass the defaults off as real
        ctx.update(error=e.message, fix=e.fix)
    except Exception as e:  # noqa: BLE001
        ctx.update(error=f"{type(e).__name__}: {e}", fix="copper_status shows the Copper config")
    return ctx


def check_sourcing(m: _Model) -> tuple[list[dict], dict]:
    from ..parts.providers import part_number
    sc = _sourcing_context(m.pr)
    if sc.get("error"):
        # which field is "the part number" and whether one is required comes from the config: do not guess
        return [finding("warning", "config_problem", f"sourcing not checked — the Copper config cannot be used: {sc['error']}",
                        sc.get("fix"))], {"sourcing": "not checked (config problem)"}
    field = sc["field"]
    out = []
    missing, unique, extended, basic, preferred, unknown = [], {}, set(), set(), set(), set()
    for ref in sorted(m.comps, key=natural_key):
        c = m.comps[ref]
        if ref.startswith("#") or c.get("dnp") or c.get("exclude_from_bom") or c.get("exclude_from_board"):
            continue
        lc = part_number(c.get("fields", {}), field)
        if m.kind.get(ref) in ("mechanical", "testpoint") and not lc:
            continue
        if not c.get("footprint"):
            continue  # reported by the footprint check
        if not lc:
            missing.append(ref)
            continue
        unique.setdefault(lc, []).append(ref)
        if not sc["classes"]:
            continue
        cls = part_class(lc, c.get("fields", {}), sc["profile"])
        if cls == "extended":
            extended.add(lc)
        elif cls == "basic":
            basic.add(lc)
        elif cls == "preferred":
            preferred.add(lc)
        else:
            unknown.add(lc)
    lcsc = (field or "").upper() == "LCSC"
    if missing and sc["required"]:
        out.append(finding("warning", "missing_lcsc" if lcsc else "missing_part_number",
                           f"{len(missing)} part(s) have no {sc['label']} ({sc['who']} needs one per part): {', '.join(missing[:25])}" +
                           (" …" if len(missing) > 25 else ""),
                           fix=("parts_source_bom() assigns LCSC parts (Basic preferred) to every unsourced component; "
                                "or sch_apply set_field LCSC per ref") if lcsc else
                               (f"parts_source_bom() fills {field} from the active parts providers; "
                                f"or sch_apply set_field {field} per ref"), items=missing))
    total = sum(len(v) for v in unique.values()) + len(missing)
    lock = round(100 * (1 - len(missing) / max(1, total)), 1)
    stats = {"placed_parts": total, "part_field": field, "unique_parts": len(unique), "missing_part_number": len(missing),
             "sourced_pct": lock if sc["required"] else 100.0}
    if lcsc:
        stats.update({"unique_lcsc": len(unique), "missing_lcsc": len(missing), "lcsc_lock_pct": lock})
    if sc["classes"]:
        fee = len(extended) * sc["fee"]
        stats.update({"basic": len(basic), "preferred": len(preferred), "extended": len(extended),
                      "unknown_class": len(unknown), "estimated_extended_fee_usd": fee})
        if extended:
            out.append(finding("info", "extended_parts",
                               f"{len(extended)} unique JLC Extended part(s) → ≈ ${fee:.0f} feeder-loading fee per order: {', '.join(sorted(extended)[:12])}",
                               fix="parts_alternatives(target='C…') to find Basic substitutes for passives/common ICs",
                               items=sorted(extended)))
    return out, stats


def part_class(number: str, fields: dict, profile: dict | None = None) -> str:
    """'basic' | 'preferred' | 'extended' | 'unknown' for a fab profile with assembly price classes
    (profile.part_classes). Only JLCPCB has them among the built-in profiles; others answer 'unknown'."""
    pc = (profile or {}).get("part_classes") if profile is not None else {"field": "JLC Class"}
    if not pc or not pc.get("field"):
        return "unknown"
    return _jlc_class(number, fields)


def _jlc_class(lcsc: str, fields: dict) -> str:
    """'basic' | 'preferred' | 'extended' | 'unknown' — shared by design_review and fab_export (jlcpcb profile).

    Order: the part's own 'JLC Class' field (written by parts_import), then the bundled catalogue, which holds
    JLC's complete Basic + Preferred lists: a C-number that is not in it is Extended (the same answer
    parts_source_bom gets from JLC live)."""
    v = field_ci(fields, "JLC Class", "JLC Type", "JLCPCB Class", "Part Class", "LCSC Class", "Basic/Extended").lower()
    if v:
        return "basic" if "basic" in v else "preferred" if "prefer" in v else "extended" if "ext" in v else "unknown"
    try:
        from ..parts import basic as _b
        rec = _b.by_lcsc(lcsc)
        if isinstance(rec, dict):
            c = str(rec.get("class") or "").lower()
            if rec.get("basic") or c == "basic":
                return "basic"
            if rec.get("preferred") or "prefer" in c:
                return "preferred"
            return "extended"
        if _b.all_parts():
            return "extended"
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def check_footprints(m: _Model) -> list[dict]:
    missing = [r for r in sorted(m.comps, key=natural_key)
               if not r.startswith("#") and not m.comps[r].get("footprint") and not m.comps[r].get("exclude_from_board")]
    if not missing:
        return []
    return [finding("error", "missing_footprint",
                    f"{len(missing)} symbol(s) have no footprint and cannot be placed on the PCB: {', '.join(missing[:25])}",
                    fix=("sch_apply(ops=[{\"op\":\"set_field\",\"ref\":\"" + missing[0] + "\",\"field\":\"Footprint\",\"value\":\"<Lib:Footprint>\"}]) "
                         "— find one with library(action='search_footprints', query=...) or parts_import(part=…)"),
                    items=missing)]


def _norm_name(n: str) -> str:
    return re.sub(r"[\s_\-./]", "", base_net(n)).upper()


def _lev1(a: str, b: str) -> bool:
    """One edit apart, ignoring the differences that are almost always deliberate."""
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        idx = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(idx) == 2:
            i, j = idx
            # transposition, but not digit swaps (AD12/AD21 are both real)
            return j == i + 1 and a[i] == b[j] and a[j] == b[i] and not (a[i].isdigit() and a[j].isdigit())
        if len(idx) != 1:
            return False
        x, y = a[idx[0]], b[idx[0]]
        if x.isdigit() or y.isdigit():
            return False                      # D0/D1, SDA1/SDA2
        if {x, y} in ({"P", "N"}, {"+", "-"}, {"H", "L"}, {"A", "B"}, {"X", "Y"}, {"T", "R"}, {"#", "-"}):
            return False                      # diff pairs, CANH/CANL, A/B, TX/RX, active-low styles
        return True
    s, l = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(l)):
        if l[:i] + l[i + 1:] == s:
            ch = l[i]
            return not ch.isdigit() and ch not in "NPB#-_"   # VCC/VCC2, RST/RSTN, CS/CSB are deliberate
    return False


def check_single_pin_nets(m: _Model) -> list[dict]:
    """Named nets with exactly one pin — the classic label typo. When another
    net's name is one edit away, the finding says which one was probably meant."""
    out = []
    named = [n for n in m.nets if not is_auto_net(n)]
    by_norm: dict = {}
    for n in named:
        by_norm.setdefault(_norm_name(n), []).append(n)
    for net, nodes in sorted(m.nets.items()):
        if is_auto_net(net) or len(nodes) != 1:
            continue
        n = nodes[0]
        key = _norm_name(net)
        cands = [c for c in by_norm.get(key, []) if c != net]
        if not cands and len(key) >= 3:
            cands = [c for k, lst in by_norm.items() if len(k) >= 3 and _lev1(key, k) for c in lst if len(m.nets[c]) > 1]
        guess = f" — did you mean {cands[0]}?" if cands else ""
        fix_net = base_net(cands[0]) if cands else base_net(net)
        out.append(finding("warning", "single_pin_net",
                           f"net {net} connects only {n['ref']}.{n['pin']}{' (' + n['pin_name'] + ')' if n.get('pin_name') else ''}{guess}",
                           fix=(f"sch_apply(ops=[{{\"op\":\"disconnect\",\"pin\":\"{n['ref']}.{n['pin']}\"}},{{\"op\":\"connect\",\"pin\":\"{n['ref']}.{n['pin']}\",\"net\":\"{fix_net}\"}}])"
                                if cands else
                                f"connect the partner pin: sch_apply(ops=[{{\"op\":\"connect\",\"pin\":\"REF.PIN\",\"net\":\"{base_net(net)}\"}}]) — or no_connect {n['ref']}.{n['pin']} if unused"),
                           items=[f"{n['ref']}.{n['pin']}"], net=net, suggestion=cands[0] if cands else None))
    return out


def check_net_names(m: _Model) -> list[dict]:
    """Different nets whose names differ only by case/separators (VCC_3V3 vs Vcc3v3)."""
    out = []
    groups: dict = {}
    for n in m.nets:
        if not is_auto_net(n):
            groups.setdefault(_norm_name(n), []).append(n)
    for key, names in groups.items():
        bases = {base_net(n) for n in names}
        if len(names) > 1 and len(bases) > 1:
            out.append(finding("warning", "net_name_collision",
                               f"nets {', '.join(names)} look like one signal (names differ only by case/separators) but are separate nets",
                               fix="use one exact name (a global label if it crosses sheets) — or rename one so the difference is obviously intentional",
                               items=names))
    return out


# ── power checks (schematic + board when there is one) ────────────────────
# Feedback reference voltages of common adjustable regulators (datasheet typ.). Vout = Vref·(1 + Rtop/Rbottom).
_VREF = [
    (r"mt3608|sx1308|sy8089|sy8088|ap3417|ap3428|fp6291|rt8059|tlv62569|tlv62568|tps6208|tps6209|mp2359a?\b", 0.6),
    (r"mp1584|tps54331|tps5410|mp1495|mp2315", 0.8),
    (r"mp2307|mp2303", 0.925),
    (r"tps563200|tps562200|tps563201", 0.768),
    (r"lmr14030|lmr14020", 0.75),
    (r"lm2596|lm2576|lm2577|lm2575|lm2733|lm2585", 1.23),
    (r"tps5430|tps5450", 1.221),
    (r"xl6009|xl6019|xl4015|xl4016|lm317|lm1117|ams1117|ld1117|ncp1117|mic29302|lm338|lm350", 1.25),
]
_FB_PIN = re.compile(r"^(?:FB|VFB|ADJ|ADJ/GND|ADJUST|FEEDBACK)$")
_POWER_IC_RE = re.compile(_REG_RE.pattern + r"|charg|tp40[5-9]\d|tp4054|bq2[45]|mcp738|ip53\d\d|ip51\d\d|drv8|a4988|tmc2[12]|"
                          r"tb6612|l298|pam8|tpa3|max9[78]|ldo|pmic|load.?switch|efuse", re.I)


def _rail_volts(net: str) -> float | None:
    """'+5V' → 5, '3V3' → 3.3, '/VOUT_12V' → 12, 'VBUS' → 5; None when the name carries no voltage."""
    b = base_net(net).upper()
    m = re.search(r"(?<![A-Z0-9])(\d+)V(\d+)(?![0-9])", b) or re.search(r"(\d+(?:\.\d+)?)V(?![0-9])", b)
    if m:
        try:
            return float(f"{m.group(1)}.{m.group(2)}") if m.lastindex == 2 else float(m.group(1))
        except ValueError:
            return None
    return 5.0 if re.fullmatch(r"\+?VBUS\w*", b) else None


def check_regulator_setpoint(m: _Model) -> list[dict]:
    """Adjustable regulators: compute Vout from the feedback divider and compare it with the rail's name."""
    out = []
    m.setpoints = []
    for ref in sorted(m.comps, key=natural_key):
        if m.kind.get(ref) != "ic" or m.comps[ref].get("dnp"):
            continue
        t = m.text(ref)
        vref = next((v for pat, v in _VREF if re.search(pat, t)), None)
        fb = [p for p in m.comp_pins.get(ref, []) if any(_FB_PIN.match(n) for n in _clean_pin(p.get("pin_name", "")))]
        if vref is None or not fb:
            continue
        fbn = fb[0]["net"]
        gnds = m.ic_grounds(ref) | m.gnd
        bottom, top = [], []
        for r in m.comps_on(fbn, "resistor"):
            pins = m.comp_pins.get(r, [])
            if len(pins) != 2 or m.comps[r].get("dnp"):
                continue
            other = next((q["net"] for q in pins if q["net"] != fbn), None)
            val = parse_value(m.comps[r].get("value", ""))
            if other is None or not val:
                continue
            (bottom if other in gnds else top).append((r, val, other))
        if len(bottom) != 1 or len(top) != 1:
            continue                       # no divider (fixed version) or a network we don't model
        (rb, vb, _), (rt, vt, rail) = bottom[0], top[0]
        vout = vref * (1 + vt / vb)
        want = _rail_volts(rail)
        rec = {"ref": ref, "vref": vref, "r_top": rt, "r_bottom": rb, "vout": round(vout, 3), "rail": rail}
        m.setpoints.append(rec)
        if want is None or abs(vout - want) / want <= 0.05:
            continue
        ideal = vb * (want / vref - 1)
        out.append(finding(
            "warning", "regulator_setpoint",
            f"{ref} ({m.comps[ref].get('value')}) feedback divider {rt}={m.comps[rt].get('value')} / {rb}={m.comps[rb].get('value')} "
            f"sets {vout:.2f} V (Vref {vref} V) but drives rail {rail} (≈{want:g} V)",
            fix=(f"Vout = {vref}·(1+Rtop/Rbottom): for {want:g} V with {rb}={m.comps[rb].get('value')} use Rtop ≈ {ideal / 1e3:.3g}k — "
                 f"sch_apply(ops=[{{\"op\":\"set_field\",\"ref\":\"{rt}\",\"field\":\"Value\",\"value\":\"{ideal / 1e3:.3g}k\"}}]) "
                 f"then parts_source_bom() to re-source it"),
            items=[f"{ref}.{fb[0]['pin']}", rt, rb], net=rail))
    return out


def _power_path_nets(m: _Model) -> dict:
    """net → why it carries real current: inductors, battery terminals, power-FET channels, switch-node diodes,
    switching-regulator power pins."""
    why: dict = {}

    def add(net, reason):
        if net and not net.startswith("unconnected-") and net not in why:
            why[net] = reason
    batt_nets = set()
    for ref, pins in m.comp_pins.items():
        k, t = m.kind.get(ref), m.text(ref)
        if m.comps.get(ref, {}).get("dnp"):
            continue
        if k == "inductor" and len(pins) == 2 and not re.search(r"ferrite|bead", t):
            for p in pins:
                add(p["net"], f"inductor {ref}")
        # the battery itself or its connector (an IC whose description mentions batteries is not a terminal)
        if ref_prefix(ref) in ("BT", "BAT") or (k in ("connector", "other") and re.search(r"\bbatt?ery\b|\bcell\b|li-?ion|lipo", t)):
            for p in pins:
                if not is_ground_net(p["net"]):
                    add(p["net"], f"battery {ref}")
                    batt_nets.add(p["net"])
    for net in list(m.nets):
        b = base_net(net).upper()
        if re.fullmatch(r"\+?V?BAT(?:T|TERY)?[+-]?|BAT[PN]|BATT?_?(?:POS|NEG|P|N)|B[+-]|CELL[+-]?|VCELL", b):
            add(net, "battery net")
            batt_nets.add(net)
    for ref, pins in m.comp_pins.items():
        if m.kind.get(ref) != "transistor":
            continue
        ds = [p for p in pins if any(re.fullmatch(r"[DS]\d?|DRAIN\d?|SOURCE\d?", n) for n in _clean_pin(p.get("pin_name", "")))]
        if ds and any(p["net"] in batt_nets or p["net"] in why for p in ds):
            for p in ds:
                add(p["net"], f"power FET {ref}")
    for ref, pins in m.comp_pins.items():
        if m.kind.get(ref) == "diode" and len(pins) == 2 and any(p["net"] in why and why[p["net"]].startswith("inductor")
                                                                 for p in pins):
            for p in pins:
                add(p["net"], f"switching diode {ref}")
    return why


def _read_board(pr) -> dict:
    """Tracks (width per net), vias, zones (nets) and footprint pads from the .kicad_pcb."""
    if pr is None or not getattr(pr, "pcb", None) or not pr.pcb.exists():
        return {}
    tree = sexpr.loads(pr.pcb.read_text(encoding="utf-8"))
    names = {}
    for n in sexpr.find_all(tree, "net"):           # KiCad ≤ 9 net table: (net 3 "GND")
        if len(n) >= 3:
            names[str(n[1])] = str(n[2])

    def net_of(node):
        v = sexpr.find(node, "net")
        if v is None or len(v) < 2:
            return ""
        return names.get(str(v[1]), str(v[1])) if len(v) == 2 else str(v[2])
    widths: dict = {}
    for kind in ("segment", "arc"):
        for t in sexpr.find_all(tree, kind):
            w = sexpr.value(t, "width")
            n = net_of(t)
            if n and w is not None:
                widths.setdefault(n, []).append(float(w))
    vias = []
    for v in sexpr.find_all(tree, "via"):
        at = sexpr.find(v, "at")
        if at is not None and len(at) >= 3:
            vias.append((float(at[1]), float(at[2]), net_of(v)))
    zones = set()
    for z in sexpr.find_all(tree, "zone"):
        attr = sexpr.find(z, "attr")
        if attr is not None and sexpr.find(attr, "teardrop") is not None:
            continue
        zones.add(net_of(z))
    fps = {}
    for fp in sexpr.find_all(tree, "footprint"):
        props = {str(q[1]): str(q[2]) for q in sexpr.find_all(fp, "property") if len(q) > 2}
        at = sexpr.find(fp, "at") or [None, 0, 0]
        x0, y0 = float(at[1]), float(at[2])
        rot = float(at[3]) if len(at) > 3 else 0.0
        pads = []
        for pd in sexpr.find_all(fp, "pad"):
            pa, sz = sexpr.find(pd, "at"), sexpr.find(pd, "size")
            if pa is None or sz is None or len(pd) < 3:
                continue
            c, s_ = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            px, py = float(pa[1]), float(pa[2])
            ang = math.radians(float(pa[3])) if len(pa) > 3 else math.radians(rot)
            w, h = float(sz[1]), float(sz[2] if len(sz) > 2 else sz[1])
            pads.append({"num": str(pd[1]), "type": str(pd[2]), "x": x0 + px * c + py * s_, "y": y0 - px * s_ + py * c,
                         "hx": abs(w / 2 * math.cos(ang)) + abs(h / 2 * math.sin(ang)),
                         "hy": abs(w / 2 * math.sin(ang)) + abs(h / 2 * math.cos(ang)), "area": w * h, "net": net_of(pd)})
        fps[props.get("Reference", "")] = {"name": str(fp[1]), "pads": pads}
    classes, patterns, assigned = {}, [], {}
    try:
        ns = json.loads(pr.pro.read_text(encoding="utf-8")).get("net_settings") or {}
        classes = {c.get("name"): c for c in ns.get("classes") or []}
        patterns = [(q.get("pattern", ""), q.get("netclass")) for q in ns.get("netclass_patterns") or []]
        assigned = ns.get("netclass_assignments") or {}
    except (OSError, ValueError, AttributeError):
        pass
    return {"widths": widths, "vias": vias, "zones": zones, "fps": fps, "classes": classes, "patterns": patterns,
            "assigned": assigned, "routed": bool(widths)}


def _netclass(b: dict, net: str) -> tuple[str, float | None]:
    """Net class KiCad gives `net` (explicit assignment, then the first matching wildcard pattern, else Default)."""
    cls = None
    a = (b.get("assigned") or {}).get(net)
    if a:
        cls = a if isinstance(a, str) else (a[0] if a else None)
    if cls is None:
        for pat, c in b.get("patterns") or []:
            if pat and fnmatch.fnmatchcase(net, pat):
                cls = c
                break
    cls = cls or "Default"
    w = (b.get("classes", {}).get(cls) or b.get("classes", {}).get("Default") or {}).get("track_width")
    return cls, (float(w) if w is not None else None)


POWER_TRACK_MIN_MM = 0.4      # ≈1 A on 1 oz outer copper at ~10 °C rise (IPC-2152); a power path wants this or a pour


def check_power_path(m: _Model) -> list[dict]:
    b = m.board()
    if not b:
        return []
    why = _power_path_nets(m)
    out = []
    narrow_routed, narrow_class = [], []
    for net, reason in sorted(why.items()):
        if net in b["zones"]:
            continue                      # poured: the copper area carries the current
        ws = b["widths"].get(net)
        cls, cw = _netclass(b, net)
        # a net the designer already put in a dedicated (non-Default) class is routed at that class's width on
        # purpose — e.g. the jlc-4layer Power class is 0.3 mm so power can leave fine-pitch pads. Only widths
        # below the net's own power class, or power paths left in Default, are findings.
        floor = min(POWER_TRACK_MIN_MM, cw) if (cls != "Default" and cw) else POWER_TRACK_MIN_MM
        if ws:
            if min(ws) < floor - 1e-6:
                narrow_routed.append((net, reason, min(ws), sum(1 for w in ws if w < floor - 1e-6), floor))
        elif cls == "Default" and cw is not None and cw < POWER_TRACK_MIN_MM - 1e-6:
            narrow_class.append((net, reason, cls, cw))
    for net, reason, w, n, floor in narrow_routed:
        out.append(finding(
            "warning", "power_path_narrow",
            f"power-path net {net} ({reason}) is routed with {n} track segment(s) as narrow as {w:g} mm — "
            f"carrying ~1 A+ it needs ≥ {floor:g} mm (or a pour)",
            fix=(f"pcb_setup(power_nets=[{json.dumps(net)}]) to put it in the Power class, then "
                 f"pcb_edit(ops=[{{\"op\":\"delete\",\"nets\":[{json.dumps(net)}]}}]) and pcb_route() to re-route it wide"),
            net=net))
    if narrow_class:
        nets = [n for n, *_ in narrow_class]
        out.append(finding(
            "warning", "power_path_netclass",
            f"{len(nets)} power-path net(s) would be routed at the {narrow_class[0][2]} class width "
            f"{narrow_class[0][3]:g} mm (no Power net-class pattern matches them): "
            + ", ".join(f"{n} ({r})" for n, r, *_ in narrow_class[:8]),
            fix=f"pcb_setup(power_nets={json.dumps(nets[:12])}) before pcb_route() — patterns match KiCad's full net names",
            items=nets))
    return out


def check_thermal_pad(m: _Model) -> list[dict]:
    """Exposed pads of power ICs need thermal vias into the inner/back copper once the board is laid out."""
    b = m.board()
    if not b or not b.get("routed"):
        return []
    out = []
    for ref in sorted(m.comps, key=natural_key):
        fp = b["fps"].get(ref)
        if m.kind.get(ref) != "ic" or not fp or len(fp["pads"]) < 5:
            continue
        smd = [q for q in fp["pads"] if q["type"] == "smd"]
        if len(smd) < 4:
            continue
        areas = sorted(q["area"] for q in smd)
        big = max(smd, key=lambda q: q["area"])
        median = areas[len(areas) // 2]
        if big["area"] < 1.5 or big["area"] < 4 * median:
            continue                     # no exposed pad
        def inside(x, y):
            return abs(x - big["x"]) <= big["hx"] and abs(y - big["y"]) <= big["hy"]
        n = sum(1 for (x, y, _) in b["vias"] if inside(x, y))
        n += sum(1 for q in fp["pads"] if q["type"] == "thru_hole" and q["num"] == big["num"] and inside(q["x"], q["y"]))
        if n:
            continue
        power = bool(_POWER_IC_RE.search(m.text(ref)))
        w, h = 2 * big["hx"], 2 * big["hy"]
        k = max(2, min(9, int((w // 1.0) * (h // 1.0))))
        out.append(finding(
            "warning" if power else "info", "thermal_pad_no_vias",
            f"{ref} ({m.comps[ref].get('value')}) exposed pad {big['num']} ({w:.1f}×{h:.1f} mm, net {big['net'] or '?'}) has no thermal vias"
            + (" — a power IC dumps its heat through this pad" if power else ""),
            fix=(f"add ~{k} vias (0.3/0.6 mm, 1 mm pitch) inside the pad: pcb_edit(ops=[{{\"op\":\"add_via\",\"at\":[{big['x']:.2f},{big['y']:.2f}],"
                 f"\"net\":{json.dumps(big['net'] or 'GND')}}}, …]) and pour {big['net'] or 'GND'} on the back (pcb_pour)"),
            items=[f"{ref}.{big['num']}"], at=[round(big["x"], 3), round(big["y"], 3)]))
    return out


# ── entry point ───────────────────────────────────────────────────────────
def review(pr, checks: list[str] | None = None, include_erc: bool = True, netlist: dict | None = None) -> dict:
    nl = netlist or load_netlist(pr)
    m = _Model(nl)
    m.pr = pr
    want = set(checks or DEFAULT_CHECKS)
    findings: list[dict] = []
    stats: dict = {}
    run = []
    table = [("decoupling", check_decoupling), ("i2c_pullups", check_i2c_pullups), ("crystal_caps", check_crystal_caps),
             ("usb_c_cc", check_usb_c_cc), ("usb_esd", check_usb_esd), ("regulator_caps", check_regulator_caps),
             ("enable_pins", check_enable_pins), ("led_resistors", check_led_resistors),
             ("footprints", check_footprints), ("single_pin_nets", check_single_pin_nets), ("net_names", check_net_names),
             ("regulator_setpoint", check_regulator_setpoint), ("power_path", check_power_path), ("thermal_pad", check_thermal_pad)]
    for name, fn in table:
        if name in want:
            run.append(name)
            try:
                findings += fn(m)
            except Exception as e:  # noqa: BLE001 — one heuristic must never sink the review
                findings.append(finding("info", "review_check_failed", f"check {name} crashed: {type(e).__name__}: {e}"))
    if "sourcing" in want:
        run.append("sourcing")
        f, stats = check_sourcing(m)
        findings += f
    style = None
    if "schematic_style" in want and pr is not None:
        run.append("schematic_style")
        try:
            from . import sch_style
            style = sch_style.check(pr)
        except Exception as e:  # noqa: BLE001 — drawing style must never sink the engineering review
            style = [finding("info", "review_check_failed", f"check schematic_style crashed: {type(e).__name__}: {e}")]
    if "power_flags" in want and include_erc:
        run.append("power_flags")
        try:
            from .verify import run_erc
            erc = run_erc(pr)
            for v in erc["violations"]:
                if v["type"] == "power_pin_not_driven":
                    findings.append(finding("error", "rail_not_driven", v["message"], fix=v["fix"], items=v.get("items"),
                                            at=v.get("at"), net=v.get("net")))
            stats["erc"] = {"errors": erc["errors"], "warnings": erc["warnings"]}
        except Exception as e:  # noqa: BLE001
            findings.append(finding("info", "review_check_failed", f"ERC for power-flag check failed: {e}"))
    for f in findings:
        _route_to_sheet(f, m)
    findings = sort_findings(findings)
    rails = sorted(n for n in m.power if not is_auto_net(n))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warning", "info")}
    stats.update({"components": len([r for r in m.comps if not r.startswith("#")]), "nets": len(m.nets),
                  "named_nets": len([n for n in m.nets if not is_auto_net(n)]), "power_rails": rails,
                  "ground_nets": sorted(m.gnd),
                  "by_kind": _count(m.kind.values())})
    if getattr(m, "setpoints", None):
        stats["regulator_setpoints"] = m.setpoints
    out = {"counts": counts, "findings": findings, "stats": stats, "checks_run": run}
    if style is not None:       # kept apart: drawing advice is never sorted in among engineering findings
        # (when both lists come back they still share one result size cap)
        out["style"] = sort_findings(style)
    return out


def _route_to_sheet(f: dict, m: _Model):
    """sch_apply edits one sheet per call: when the finding's part lives on a
    sub-sheet, put sheet='<name>' into the suggested call."""
    fix = f.get("fix") or ""
    if "sch_apply(ops=" not in fix:
        return
    for it in f.get("items") or []:
        ref = (it if isinstance(it, str) else (it.get("ref") or "")).split(".")[0]
        sp = (m.comps.get(ref) or {}).get("sheet") or "/"
        name = sp.strip("/").split("/")[-1] if sp.strip("/") else ""
        if name:
            f["fix"] = fix.replace("sch_apply(ops=", f'sch_apply(sheet="{name}", ops=')
            f["sheet"] = sp
        return


def _count(vals) -> dict:
    d: dict = {}
    for v in vals:
        d[v] = d.get(v, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))
