"""Stage idioms: sub-circuits around an IC that have ONE canonical drawing.

    opamp     feedback network as a loop ABOVE the body (the unit is posed with its inverting input on top), a
              follower as a plain wire loop, the gain leg on the loop row, the input resistor on the input row
    boost     VIN - L - SW node - D - VOUT on one horizontal line, left to right; input caps, feedback divider and
              output caps hang from it; the controller sits below, its SW pin wired straight up to the SW node
    buck      SW pin - (catch diode down) - L - VOUT, divider and output caps hanging, on the SW pin's row
    ldo       caps hanging on the IN and OUT pin rows, supply symbols up at both ends, the grounds on one row
    crystal   crystal between two drops from the oscillator pins, load caps to ONE shared ground symbol, an
              optional series resistor in line on its pin row
    usbc      the two CC pull-downs in line on their pin rows, joined to ONE ground symbol
    tswitch   a transistor posed control-left, source / emitter towards its rail (pose only; its resistors are
              node idioms)
    peer      a second IC / connector that shares nets with the block's main IC, posed facing it (dock.py)

Matchers are read-only over `Design` and never look at coordinates; template builders are pure functions of the
match and the host pin tips. `model.instantiate` proves every drawing against the netlist; a failed proof drops
the match and its parts are drawn the legacy way.
"""
from __future__ import annotations

import math
import re

from ..blocks import _refkey, prefix
from ..build import GAP, STUB, U, hit, pins, pose_pin, power_box, set_pose, snap, sym_boxes
from .match import ends, is_led, two_pin
from .model import IdiomError, Match, Template
from .passive import _hang, _inline, _label_room, _pitch, _roles

OWN_GROUND = True          # a 4-pin crystal's case pin that lies above its side's signal pin gets its own ground symbol
STAGES = ("opamp", "boost", "buck", "ldo", "crystal", "usbc", "tswitch", "peer")
_INV_NAME = {"-", "−", "IN-", "IN−", "INV", "-IN", "INN", "VIN-", "V-IN"}
_IN_PIN = re.compile(r"^(VIN|IN|VI|INPUT)\d*$", re.I)
_OUT_PIN = re.compile(r"^(VOUT|OUT|VO|OUTPUT)\d*$", re.I)
# (class I) a regulator's feedback / sense pin that sits directly on the output rail (a fixed-output converter): it
# is wired to the output node of the stage, never drawn as one more supply symbol
SENSE_WIRE = True
_SENSE_PIN = re.compile(r"^(FB|VFB|SENSE|VSENSE|VOS|VOUT_?S|OUT_?S|ADJ)$", re.I)
_CTRL = {"G", "B", "GATE", "BASE"}
_LOW = {"S", "E", "SOURCE", "EMITTER"}
_HIGH = {"D", "C", "DRAIN", "COLLECTOR"}


def _pool(D, info, claimed) -> list:
    return [r for r in info.sats if r not in claimed]


def _between(D, pool, pfx, a, b) -> list:
    return sorted((r for r in pool if prefix(r) in pfx and two_pin(D, r) and set(D.pins_of[r].values()) == {a, b}), key=_refkey)


def _to_gnd(D, pool, pfx, a) -> list:
    return sorted((r for r in pool if prefix(r) in pfx and two_pin(D, r) and a in D.pins_of[r].values()
                   and D.is_gnd(ends(D, r, a)[2])), key=_refkey)


def _ics(D, info) -> list:
    return [a for a in info.anchors if not D.is_conn(a)]


def _host_pin(D, info, k, ics=None):
    """(ref, pin number) of the first IC pin of the block on net k."""
    for a in (ics if ics is not None else _ics(D, info)):
        for p in sorted(D.pins_of[a], key=lambda p: _refkey(str(p))):
            if D.pins_of[a][p] == k:
                return (a, p)
    return None


# ───────────────────────── matchers ─────────────────────────
def unit_pins(D, ref, ui) -> list:
    """[(pin number, net, electrical type, pin name)] of one unit, every pin once."""
    out, seen = [], set()
    for p in D.units[ref][ui].pins_world():
        if p["number"] in seen:
            continue
        seen.add(p["number"])
        out.append((p["number"], D.pins_of[ref].get(p["number"]), str(D.ptypes.get((ref, p["number"]), "")),
                    str(p.get("name", ""))))
    return out


def opamp_matches(D, info, claimed: set) -> list:
    """Per amplifier unit: the 2-pin parts between its output and one input are the feedback; that input is the
    inverting one (an input wired straight to the output makes a follower)."""
    out = []
    for ref in list(info.anchors) + [r for r in info.sats if r not in claimed]:
        if D.is_conn(ref) or ref not in D.units:
            continue
        for ui in range(len(D.units[ref])):
            ps = [q for q in unit_pins(D, ref, ui) if "power" not in q[2]]
            outs = [q for q in ps if q[2] == "output"]
            ins = [q for q in ps if q[2] == "input"]
            if len(ps) != 3 or len(outs) != 1 or len(ins) != 2:
                continue
            O = outs[0][1]
            if not D.conn(O) or D.is_rail(O):
                continue
            pool = _pool(D, info, claimed)
            best = None
            for no, k, _t, nm in ins:
                if not D.conn(k) or D.is_rail(k):
                    continue
                fol = k == O
                fb = [] if fol else _between(D, pool, ("R", "C"), k, O)[:2]
                if not fol and not fb:
                    continue
                sc = (2 if fol else 1) + (1 if nm.strip().upper() in _INV_NAME else 0)
                if best is None or sc > best[0]:
                    best = (sc, no, k, fol, fb)
            if best is None:
                continue
            _sc, inv, K, fol, fb = best
            ni = next(q[0] for q in ins if q[0] != inv)
            roles = {f"fb{i}": r for i, r in enumerate(fb)}
            nets = {"OUT": O, "INV": K}
            if not fol:
                rest = [r for r in pool if r not in fb and two_pin(D, r) and K in D.pins_of[r].values()
                        and prefix(r) in ("R", "C")]
                legs = [r for r in rest if D.is_rail(ends(D, r, K)[2])][:1]
                for r in legs:
                    roles["leg0"] = r
                    nets["leg0"] = ends(D, r, K)[2]
                ser = [r for r in rest if r not in legs and not D.is_rail(ends(D, r, K)[2])
                       and D.conn(ends(D, r, K)[2]) and ends(D, r, K)[2] != O]
                if len(ser) == 1:
                    roles["in"] = ser[0]
                    nets["IN"] = ends(D, ser[0], K)[2]
            drawn_k = {(ref, inv)} | {(r, p) for r in roles.values() for p, k in D.pins_of[r].items() if k == K}
            drawn_o = {(ref, outs[0][0])} | {(r, p) for r in roles.values() for p, k in D.pins_of[r].items() if k == O}
            if fol:
                drawn_o |= drawn_k
            extra = {"out": outs[0][0], "inv": inv, "ni": ni, "follower": fol,
                     "inv_open": bool(set(D.view.nets[K].pins) - drawn_k) and not fol,
                     "out_open": bool(set(D.view.nets[O].pins) - drawn_o)}
            out.append(Match("opamp", roles, nets, (ref, ui), 100, extra))
            claimed.update(roles.values())
    return out


RAIL_LED = True            # fix round 7: the "power on" LED of a regulator hangs on its output line (was an island of two supply symbols)


def _rail_led(D, pool, vout, roles: dict, nets: dict):
    """A resistor and an LED in series from the output rail to ground (the indicator of that rail): roles pl0 (on
    the rail), pl1 (to ground), nets PL (the net between them), pl1 (ground)."""
    if not RAIL_LED or not D.is_rail(vout):
        return
    for e1 in sorted((r for r in pool if two_pin(D, r) and prefix(r) in ("R", "D", "LED") and vout in D.pins_of[r].values()), key=_refkey):
        M = ends(D, e1, vout)[2]
        if D.is_rail(M) or not D.conn(M) or len(D.view.nets[M].pins) != 2:
            continue
        e2 = [r for r in pool if r != e1 and two_pin(D, r) and M in D.pins_of[r].values() and D.is_gnd(ends(D, r, M)[2])]
        if len(e2) != 1 or {prefix(e1) == "R", prefix(e2[0]) == "R"} != {True, False}                 or not is_led(D, e2[0] if prefix(e1) == "R" else e1):
            continue
        roles["pl0"], roles["pl1"] = e1, e2[0]
        nets["PL"], nets["pl1"] = M, ends(D, e2[0], M)[2]
        return


def _rail_led_draw(T: Template, D, m: Match, net_role: str, x, y, taken: list):
    """The indicator chain as one column under the rail's row at x. -> boxes"""
    for _attempt in range(12):
        fr, bx, _ = _hang(T, D, "pl0", m.roles["pl0"], m.nets[net_role], x, y, 1, lead=0, slot="right")
        tap = (fr[0], snap(fr[1] + 2 * U))
        f2, bx2, M = _hang(T, D, "pl1", m.roles["pl1"], m.nets["PL"], tap[0], tap[1], 1, lead=0, slot="right")
        q = (f2[0], snap(f2[1] + U))
        col = bx + bx2 + [power_box(D.text(M), q, "down")]
        if not any(hit(a, b, -0.6) for a in col for b in taken):
            break
        x = snap(x + 2 * U)
    T.wires += [("PL", [fr, tap]), ("pl1", [f2, q])]
    T.powers.append(("pl1", q, "down"))
    if D.named(m.nets["PL"]):
        T.labels["PL"] = (tap[0], snap(fr[1] + U), (-1, 0))
    taken += col
    return x, col


def power_matches(D, info, claimed: set) -> list:
    """Switching converters: an inductor on a switch pin of an IC. A series diode from the switch node makes it a
    boost (VIN - L - SW - D - VOUT), otherwise it is a buck (SW - L - VOUT, optional catch diode to ground)."""
    out = []
    ics = _ics(D, info)
    for L in sorted((r for r in _pool(D, info, claimed) if prefix(r) == "L" and two_pin(D, r)), key=_refkey):
        (_p1, n1), (_p2, n2) = sorted(D.pins_of[L].items())
        for SW, X in ((n1, n2), (n2, n1)):
            if L in claimed or D.is_rail(SW) or not D.conn(SW) or not D.conn(X):
                continue
            host = _host_pin(D, info, SW, ics)
            if host is None:
                continue
            pool = [r for r in _pool(D, info, claimed) if r != L]
            dio = [r for r in pool if prefix(r) == "D" and two_pin(D, r) and not is_led(D, r) and SW in D.pins_of[r].values()]
            ser = [r for r in dio if not D.is_gnd(ends(D, r, SW)[2]) and ends(D, r, SW)[2] != X]
            catch = [r for r in dio if D.is_gnd(ends(D, r, SW)[2])]
            others = {r for r, _p in D.view.nets[SW].pins} - {host[0], L} - set(dio)
            if any(prefix(r) != "C" for r in others):
                continue                                   # something else hangs on the switch node
            roles, nets = {"L": L}, {"SW": SW}
            if ser:
                kind, vin, vout = "boost", X, ends(D, ser[0], SW)[2]
                roles["D"] = ser[0]
                nets["VIN"] = vin
            else:
                if D.is_gnd(X):
                    continue
                kind, vin, vout = "buck", None, X
                if catch:
                    roles["D"] = catch[0]
                    nets["D"] = ends(D, catch[0], SW)[2]
            if not D.conn(vout) or D.is_gnd(vout):
                continue
            nets["VOUT"] = vout
            pool = [r for r in pool if r not in roles.values()]
            for rt in [r for r in pool if prefix(r) == "R" and two_pin(D, r) and vout in D.pins_of[r].values()]:
                F = ends(D, rt, vout)[2]
                if D.is_rail(F) or not D.conn(F) or F not in D.pins_of[host[0]].values():
                    continue
                rb = [r for r in _to_gnd(D, pool, ("R",), F) if r != rt]
                if rb:
                    roles["rt"], roles["rb"] = rt, rb[0]
                    nets["FB"], nets["rb"] = F, ends(D, rb[0], F)[2]
                    break
            pool = [r for r in pool if r not in roles.values()]
            for i, r in enumerate(_to_gnd(D, pool, ("C",), vout)[:3]):
                roles[f"cout{i}"] = r
                nets[f"cout{i}"] = ends(D, r, vout)[2]
            _rail_led(D, [r for r in pool if r not in roles.values()], vout, roles, nets)
            if kind == "boost":
                for i, r in enumerate(_to_gnd(D, pool, ("C",), vin)[:3]):
                    roles[f"cin{i}"] = r
                    nets[f"cin{i}"] = ends(D, r, vin)[2]
            extra = {}
            sense = [p_ for p_, k_ in D.pins_of[host[0]].items() if k_ == vout and p_ != host[1]
                     and _SENSE_PIN.match(str(D.pin_name.get((host[0], p_), "")).strip())]
            if SENSE_WIRE and D.is_rail(vout) and len(sense) == 1:
                extra["sense"] = sense[0]
            out.append(Match(kind, roles, nets, host, 110, extra))
            claimed.update(roles.values())
    return out


def ldo_matches(D, info, claimed: set, skip=()) -> list:
    """A small regulator with an IN and an OUT pin on two different supply rails and caps to ground on them."""
    out = []
    for a in _ics(D, info):
        if a in skip or D.npins(a) > 8 or len(D.units[a]) != 1:
            continue
        pin_in = [p for p in D.pins_of[a] if _IN_PIN.match(str(D.pin_name.get((a, p), "")))]
        pin_out = [p for p in D.pins_of[a] if _OUT_PIN.match(str(D.pin_name.get((a, p), "")))]
        if len(pin_in) != 1 or not pin_out:
            continue
        ki, ko = D.pins_of[a][pin_in[0]], D.pins_of[a][pin_out[0]]
        if any(D.pins_of[a][p] != ko for p in pin_out):
            continue
        if ki == ko or not all(D.is_rail(k) and not D.is_gnd(k) for k in (ki, ko)):
            continue
        tips = {tuple(q["xy"]) for q in D.units[a][0].pins_world() if q["number"] in pin_out}
        if len(tips) != 1:
            continue                                       # output pins that are not one stacked pin
        pool = [r for r in _pool(D, info, claimed) if D.role.get(r, {}).get("anchor") in (a, None)]
        roles, nets = {}, {"IN": ki, "OUT": ko}
        for tag, k in (("ci", ki), ("co", ko)):
            for i, r in enumerate(_to_gnd(D, pool, ("C",), k)[:2]):
                roles[f"{tag}{i}"] = r
                nets[f"{tag}{i}"] = ends(D, r, k)[2]
        if not roles:
            continue
        _rail_led(D, [r for r in pool if r not in roles.values()], ko, roles, nets)
        out.append(Match("ldo", roles, nets, (a, pin_in[0]), 85, {"in": pin_in[0], "out": sorted(pin_out, key=str)[0]}))
        claimed.update(roles.values())
    return out


def crystal_matches(D, info, claimed: set) -> list:
    """A crystal between two pins of one IC (each directly or through one series resistor), with its load caps."""
    out = []
    ics = _ics(D, info)
    for y in sorted((r for r in _pool(D, info, claimed) if prefix(r) in ("Y", "X") and len(D.units[r]) == 1), key=_refkey):
        po = D.pins_of[y]
        sig = sorted({k for k in po.values() if D.conn(k) and not D.is_rail(k)}, key=str)
        if len(sig) != 2 or any(D.conn(k) and D.is_rail(k) and not D.is_gnd(k) for k in po.values()):
            continue
        if any(sum(1 for k in po.values() if k == s) != 1 for s in sig):
            continue
        pool = [r for r in _pool(D, info, claimed) if r != y]
        roles, nets, hosts = {"Y": y}, {}, {}
        gnds = {k for k in po.values() if D.is_gnd(k)}
        for tag, k in zip("AB", sig):
            nets["N" + tag] = k
            host, hk = _host_pin(D, info, k, ics), k
            if host is None:
                for r in [r for r in pool if prefix(r) == "R" and two_pin(D, r) and k in D.pins_of[r].values()]:
                    f = ends(D, r, k)[2]
                    h = _host_pin(D, info, f, ics)
                    if h is not None and not D.is_rail(f):
                        roles["R" + tag], hk, host = r, f, h
                        break
            if host is None:
                break
            nets["H" + tag] = hk
            hosts[tag] = host
            for c in _to_gnd(D, pool, ("C",), k)[:1]:
                roles["C" + tag] = c
                gnds.add(ends(D, c, k)[2])
        if len(hosts) != 2 or hosts["A"][0] != hosts["B"][0] or hosts["A"][1] == hosts["B"][1] or len(gnds) > 1:
            continue
        if gnds:
            nets["GND"] = next(iter(gnds))
        out.append(Match("crystal", roles, nets, hosts["A"], 95, {"pins": {t: hosts[t][1] for t in "AB"}}))
        claimed.update(roles.values())
    return out


def usbc_matches(D, info, claimed: set) -> list:
    """A connector's two CC pins, each with one pull-down resistor to ground and nothing else."""
    out = []
    for j in info.anchors:
        if not D.is_conn(j):
            continue
        cc = sorted((p for p in D.pins_of[j] if str(D.pin_name.get((j, p), "")).upper().startswith("CC")), key=str)
        nets_cc = []
        for p in cc:
            k = D.pins_of[j][p]
            if D.conn(k) and not D.is_rail(k) and k not in [n for n, _p in nets_cc]:
                nets_cc.append((k, p))
        if len(nets_cc) != 2:
            continue
        pool = _pool(D, info, claimed)
        roles, nets, hp, gnds = {}, {}, {}, set()
        for i, (k, p) in enumerate(nets_cc):
            rs = _to_gnd(D, pool, ("R",), k)
            if len(rs) != 1 or {r for r, _q in D.view.nets[k].pins} != {j, rs[0]}:
                break
            roles[f"r{i}"], nets[f"C{i}"], hp[str(i)] = rs[0], k, p
            gnds.add(ends(D, rs[0], k)[2])
        if len(roles) != 2 or len(gnds) != 1:
            continue
        nets["GND"] = next(iter(gnds))
        out.append(Match("usbc", roles, nets, (j, nets_cc[0][1]), 75, {"pins": hp}))
        claimed.update(roles.values())
    return out


def match_block(D, info, claimed: set) -> dict:
    """Stage matches of one block, highest priority first; `claimed` receives every part they take.
    -> {'opamp': {(ref, unit index): m}, 'power': {ic: m}, 'ldo': {ic: m}, 'crystal': [m], 'usbc': {connector: m}}"""
    out = {"opamp": {}, "power": {}, "ldo": {}, "crystal": [], "usbc": {}}
    for m in power_matches(D, info, claimed):
        out["power"].setdefault(m.host[0], m)
    for m in opamp_matches(D, info, claimed):
        out["opamp"][m.host] = m
    out["crystal"] = crystal_matches(D, info, claimed)
    for m in ldo_matches(D, info, claimed, skip=set(out["power"])):
        out["ldo"][m.host[0]] = m
    for m in usbc_matches(D, info, claimed):
        out["usbc"][m.host[0]] = m
    return out


def all_matches(sm: dict) -> list:
    return list(sm["power"].values()) + list(sm["opamp"].values()) + list(sm["crystal"]) + list(sm["ldo"].values()) \
        + list(sm["usbc"].values())


# ───────────────────────── poses ─────────────────────────
def pose_lr(si, left: list, right: list, upper=None, lower=None):
    """(rot, mirror) with the pins `left` pointing left and `right` pointing right (and pin `upper` above pin
    `lower`), the pose the symbol has now first. None when the symbol has no such pose."""
    cands = [(si.rot, si.mirror)] + [(r, m) for r in (0, 180) for m in (None, "x", "y")]
    keep = (si.at[0], si.at[1], si.rot, si.mirror)
    found = None
    for rot, mir in cands:
        set_pose(si, 0, 0, rot, mir)
        ps = pins(si)
        if any(n not in ps for n in list(left) + list(right)):
            break
        if all(tuple(ps[n]["out"]) == (-1, 0) for n in left) and all(tuple(ps[n]["out"]) == (1, 0) for n in right) \
                and (upper is None or ps[upper]["xy"][1] < ps[lower]["xy"][1] - 0.01):
            found = (rot, mir)
            break
    set_pose(si, keep[0], keep[1], keep[2], keep[3])
    return found


def transistor_pose(D, ref):
    """(rot, mirror) for a discrete transistor: control pin to the left, source / emitter down (up when it sits
    on a supply rail: a high-side switch), drain / collector opposite. None for anything else."""
    if prefix(ref) not in ("Q", "T", "M") or len(D.units.get(ref, ())) != 1:
        return None
    si = D.units[ref][0]
    names = {no: str(p.get("name", "")).upper() for no, p in pins(si).items()}
    ctrl = [no for no, n in names.items() if n in _CTRL]
    low = [no for no, n in names.items() if n in _LOW]
    high = [no for no, n in names.items() if n in _HIGH]
    if len(ctrl) != 1 or not low or not high or len(ctrl) + len(low) + len(high) != len(names):
        return None
    kl = D.pins_of[ref].get(low[0])
    ld = (0, -1) if (D.is_rail(kl) and not D.is_gnd(kl)) else (0, 1)
    keep = (si.at[0], si.at[1], si.rot, si.mirror)
    found = None
    for face in ((-1, 0), (1, 0)):
        for rot, mir in [(keep[2], keep[3])] + [(r, m) for r in (0, 90, 180, 270) for m in (None, "x", "y")]:
            set_pose(si, 0, 0, rot, mir)
            ps = pins(si)
            if tuple(ps[ctrl[0]]["out"]) == face and all(tuple(ps[n]["out"]) == ld for n in low) \
                    and all(tuple(ps[n]["out"]) == (0, -ld[1]) for n in high):
                found = (rot, mir)
                break
        if found:
            break
    set_pose(si, keep[0], keep[1], keep[2], keep[3])
    return found


# ───────────────────────── templates ─────────────────────────
def opamp_template(D, m: Match, ot, it, k: int, top: float):
    """Feedback loop above the body.  ot / it = tips of the output and the inverting input (which is the upper
    input pin and points left), top = upper edge of the unit's body and texts, k = height of the loop row.

            GND ─[ Rg ]─┬──[ Rf ]──┐
                        │   |\\     │
        IN ─[ Rin ]─────┴───|- \\___┴── OUT
                         ───|+ /
    """
    T = Template()
    (ox, oy), (ix, iy) = ot, it
    fol = m.extra["follower"]
    inv = "OUT" if fol else "INV"
    xn = snap(ix - 4 * U)
    yl = snap(iy - (math.ceil(max(0.0, iy - top) / (2 * U)) * 2 + k) * U)
    xt = snap(ox + 2 * U)
    rows, y = [], yl
    for role in _roles(m, "fb"):
        far, _bx, _ = _inline(T, D, role, m.roles[role], m.nets["INV"], 0.0, y, 1)
        rows.append((role, y, far[0]))
        y = snap(y - 6 * U)
    need = max([ln for _r, _y, ln in rows] or [0.0]) + 4 * U
    if xt - xn < need - 0.01:
        xt = snap(xn + math.ceil(need / (2 * U)) * 2 * U)
    ytop = rows[-1][1] if rows else yl
    for role, y, ln in rows:
        xl = snap(xn + math.floor(((xt - xn) - ln) / 2 / (2 * U)) * 2 * U)
        far, _bx, _ = _inline(T, D, role, m.roles[role], m.nets["INV"], xl, y, 1)
        T.wires += [(inv, [(xn, y), (xl, y)]), ("OUT", [far, (xt, y)])]
    T.wires.append((inv, [(ix, iy), (xn, iy), (xn, ytop)]))
    if fol:
        T.wires.append(("OUT", [(xn, yl), (xt, yl)]))
    T.wires.append(("OUT", [(ox, oy), (xt, oy), (xt, ytop)]))
    if m.extra["out_open"]:
        xe = snap(xt + 2 * U)
        T.wires.append(("OUT", [(xt, oy), (xe, oy)]))
        T.ports["OUT"] = (xe, oy, (1, 0))
    xs = snap(xn - 2 * U)
    if "leg0" in m.roles:
        far, _bx, M = _inline(T, D, "leg0", m.roles["leg0"], m.nets["INV"], xs, yl, -1)
        gnd = D.is_gnd(M)
        end = (snap(far[0] - STUB), yl)
        q = (end[0], snap(yl + (GAP if gnd else -GAP)))
        T.wires += [("INV", [(xn, yl), (xs, yl)]), ("leg0", [far, end, q])]
        T.powers.append(("leg0", q, "down" if gnd else "up"))
    if "in" in m.roles:
        far, _bx, _M = _inline(T, D, "in", m.roles["in"], m.nets["INV"], xs, iy, -1)
        end = (snap(far[0] - STUB), iy)
        T.wires += [("INV", [(xn, iy), (xs, iy)]), ("IN", [far, end])]
        T.ports["IN"] = (end[0], end[1], (-1, 0))
        T.flow = ("IN", "OUT")
    elif m.extra["inv_open"]:
        T.wires.append(("INV", [(xn, iy), (xs, iy)]))
        T.ports["INV"] = (xs, iy, (-1, 0))
    if D.named(m.nets["INV"]) and not fol and "INV" not in T.ports:
        T.labels["INV"] = (snap(ix - U), iy, (-1, 0)) if not rows else (xn, rows[0][1], (1, 0))
    if D.named(m.nets["OUT"]) and "OUT" not in T.ports:
        T.labels["OUT"] = (snap(ox + U), oy, (1, 0))
    return T


def _shunts(T: Template, D, m: Match, roles: list, net_role: str, x, y, d: int, taken: list):
    """Parts to ground hanging from the row, one column each, their pin ON the row. -> (next x, x of the last)"""
    last = None
    for role in roles:
        for _attempt in range(12):
            far, bx, M = _hang(T, D, role, m.roles[role], m.nets[net_role], x, y, 1, lead=0,
                               slot="right" if d > 0 else "left")
            q = (far[0], snap(far[1] + U))
            col = bx + [power_box(D.text(M), q, "down")]
            if not any(hit(a, b, -0.6) for a in col for b in taken):
                break
            x = snap(x + d * 2 * U)
        T.wires.append((role, [far, q]))
        T.powers.append((role, q, "down"))
        taken += col
        last = x
        x = snap(x + d * _pitch(bx, x, d))
    return x, last


def chain_template(D, m: Match):
    """The power line of a switching converter, signal flow left to right, origin = the switch node.

      boost:  VIN ─┬──[ L ]──●──[ D >]──┬─────┬── VOUT↑        buck:  SW ●──┬──[ L ]──┬─────┬── VOUT↑
                  [Cin]      SW        [Rt]  [Cout]                        [D]       [Rt]  [Cout]
                   ⏚         │      FB─┤      ⏚                             ⏚     FB─┤      ⏚
                             IC        [Rb]                                          [Rb]
    The SW port points down (boost: the controller sits below) or left (buck: the line starts at the SW pin)."""
    T = Template()
    taken: list = []
    if m.idiom == "boost":
        far, bx, _ = _inline(T, D, "L", m.roles["L"], m.nets["SW"], -2 * U, 0.0, -1)
        taken += bx
        last = far[0]
        cins = _roles(m, "cin")
        if cins:
            _nx, last = _shunts(T, D, m, cins, "VIN", snap(far[0] - 2 * 2 * U), 0.0, -1, taken)
        if D.is_rail(m.nets["VIN"]):
            e = (snap(last - 2 * 2 * U), 0.0)
            T.powers.append(("VIN", e, "up"))
        else:
            e = (snap(last - max(STUB, _label_room(D, m.nets["VIN"]) * U)), 0.0)
            T.ports["VIN"] = (e[0], e[1], (-1, 0))
        T.wires.append(("VIN", [far, e]))
        T.wires.append(("SW", [(-2 * U, 0.0), (2 * U, 0.0)]))
        T.ports["SW"] = (0.0, 0.0, (0, 1))
        node, bx, _ = _inline(T, D, "D", m.roles["D"], m.nets["SW"], 2 * U, 0.0, 1)
        taken += bx
    else:
        T.ports["SW"] = (0.0, 0.0, (-1, 0))
        xl = snap(max(2, _label_room(D, m.nets["SW"])) * U)
        if "D" in m.roles:
            fd, bx, M = _hang(T, D, "D", m.roles["D"], m.nets["SW"], xl, 0.0, 1, lead=0, slot="right")
            q = (fd[0], snap(fd[1] + U))
            T.wires.append(("D", [fd, q]))
            T.powers.append(("D", q, "down"))
            taken += bx + [power_box(D.text(M), q, "down")]
            xl = snap(xl + _pitch(bx, xl, 1))
        node, bx, _ = _inline(T, D, "L", m.roles["L"], m.nets["SW"], xl, 0.0, 1)
        taken += bx
        T.wires.append(("SW", [(0.0, 0.0), (xl, 0.0)]))
    x = snap(node[0] + 2 * 2 * U)
    last = node[0]
    if "rt" in m.roles:
        for _attempt in range(10):
            fr, bx, _ = _hang(T, D, "rt", m.roles["rt"], m.nets["VOUT"], x, 0.0, 1, lead=0, slot="right")
            if not any(hit(a, b, -0.6) for a in bx for b in taken):
                break
            x = snap(x + 2 * U)
        tap = (fr[0], snap(fr[1] + 2 * U))
        fb2, bx2, M = _hang(T, D, "rb", m.roles["rb"], m.nets["FB"], tap[0], tap[1], 1, lead=0, slot="right")
        q = (fb2[0], snap(fb2[1] + U))
        port = (snap(tap[0] - 2 * U), tap[1])
        T.wires += [("FB", [fr, tap, port]), ("rb", [fb2, q])]
        T.ports["FB"] = (port[0], port[1], (-1, 0))
        T.powers.append(("rb", q, "down"))
        taken += bx + bx2 + [power_box(D.text(M), q, "down")]
        last = x
        x = snap(x + _pitch(bx + bx2, x, 1))
    couts = _roles(m, "cout")
    if couts:
        _nx, last = _shunts(T, D, m, couts, "VOUT", x, 0.0, 1, taken)
    if "pl0" in m.roles:
        if couts:
            x = _nx
        last, col = _rail_led_draw(T, D, m, "VOUT", x, 0.0, taken)
    if D.is_rail(m.nets["VOUT"]):
        e = (snap(last + 2 * 2 * U), 0.0)
        T.powers.append(("VOUT", e, "up"))
    else:
        e = (snap(last + max(STUB, _label_room(D, m.nets["VOUT"]) * U)), 0.0)
        T.ports["VOUT"] = (e[0], e[1], (1, 0))
    T.wires.append(("VOUT", [node, e]))
    T.flow = ("VIN" if m.idiom == "boost" else "SW", "VOUT")
    return T


def ldo_template(D, m: Match, tin, tout, k: int):
    """Linear regulator: the caps hang on the rows of the IN pin (to the left) and the OUT pin (to the right), the
    supply symbols stand at both outer ends, every ground symbol on ONE row.

        +12V↑                    +5V↑
          └──┬───[ IN  OUT ]───┬──┘
            [C]     GND       [C]
             ⏚       ⏚         ⏚        """
    T = Template()
    fars = []
    for tag, (px, py), d in (("ci", tin, -1), ("co", tout, 1)):
        net = "IN" if tag == "ci" else "OUT"
        x = snap(px + d * k * U)
        lastx = None
        for role in _roles(m, tag):
            far, bx, _ = _hang(T, D, role, m.roles[role], m.nets[net], x, py, 1, lead=0, slot="right" if d > 0 else "left")
            fars.append((role, far))
            lastx = x
            x = snap(x + d * _pitch(bx, x, d))
        if tag == "co" and "pl0" in m.roles:
            lastx, _col = _rail_led_draw(T, D, m, "OUT", x, py, [])
        e = (snap(lastx + d * 2 * 2 * U), py) if lastx is not None else (snap(px + d * k * U), py)
        q = (e[0], snap(py - GAP))
        T.wires.append((net, [(px, py), e, q]))
        T.powers.append((net, q, "up"))
    if fars:
        yg = snap(max(f[1] for _r, f in fars) + U)
        for role, f in fars:
            T.wires.append((role, [f, (f[0], yg)]))
            T.powers.append((role, (f[0], yg), "down"))
    T.flow = ("IN", "OUT")
    return T


def crystal_template(D, m: Match, tips: dict, s: int, k: int, e: float, mirror=None, xo: int = 0, jog: bool = False):
    """Crystal at two oscillator pins on one side of an IC. tips = {'A': tip, 'B': tip} of the host pins (they
    point s = +1 right / -1 left), k = how far below the lower pin row the crystal sits, e = lead between the
    crystal's pins and the two drops.

        pin ───[ Rs ]────────┐          the upper row takes the outer drop, the lower row the inner one;
        pin ──────┐          │          the load caps hang from the two crystal nodes and share ONE ground
                  ├──[XTAL]──┤          symbol, centred; ground pins of a 4-pin crystal join that ground bar
                 [C]        [C]
                  └────⏚────┘

    `jog` (class I, a 4-pin crystal whose case pin lies ABOVE the signal pin of its side - the pins of a box symbol
    are diagonal): instead of leading the case pin across the signal's drop, the signal steps down right at its pin
    and runs out UNDER a pocket in which the case pin ends in its own ground symbol. Nothing crosses.

                 ┌────── to the pin row          4 ──────┐
        4 ───────┼──┐   (crossing)        or     1 ─┐    ⏚      ┌── to the pin row
        1 ───────┤  ⏚                               └───────────┤
                [C]                                            [C]            """
    T = Template()
    up, lo = sorted("AB", key=lambda t: (tips[t][1], t))
    start = {}
    for t in (up, lo):
        px, py = tips[t]
        room = _label_room(D, m.nets["H" + t])
        T.labels["H" + t] = (snap(px + s * U), py, (s, 0))
        if "R" + t in m.roles:
            xs = snap(px + s * room * U)
            far, _bx, _ = _inline(T, D, "R" + t, m.roles["R" + t], m.nets["H" + t], xs, py, s)
            T.wires.append(("H" + t, [(px, py), (xs, py)]))
            start[t] = (far, far[0])
            T.labels["N" + t] = (far[0], far[1], (s, 0))
        else:
            start[t] = ((px, py), snap(px + s * room * U))
    start = {t: (a, snap(x + s * xo * U)) for t, (a, x) in start.items()}      # further out, past what stands there
    yref = m.roles["Y"]
    si = D.units[yref][0]
    po = D.pins_of[yref]
    pl = next(p for p in sorted(po, key=str) if po[p] == m.nets["N" + lo])
    pu = next(p for p in sorted(po, key=str) if po[p] == m.nets["N" + up])
    xn = start[lo][1]
    yc = snap(tips[lo][1] + k * U)
    for _attempt in range(2):
        if not pose_pin(si, pl, (snap(xn + s * e), yc), (-s, 0), mirror):
            raise IdiomError("crystal: no pose with its pins along the row")
        ps = pins(si)
        if tuple(ps[pu]["out"]) != (s, 0):
            raise IdiomError("crystal: its two pins do not face away from each other")
        xf = snap(ps[pu]["xy"][0] + s * e)
        lack = (start[up][1] - xf) * s
        if lack <= 0.01:
            break
        xn = snap(xn + s * math.ceil(lack / (2 * U)) * 2 * U)
    T.parts["Y"] = (pl, (snap(xn + s * e), yc), (-s, 0), "auto", mirror)
    a_lo = (snap(ps[pl]["xy"][0]), snap(ps[pl]["xy"][1]))
    a_up = (snap(ps[pu]["xy"][0]), snap(ps[pu]["xy"][1]))
    if a_up[1] < tips[lo][1] + 2 * U or a_lo[1] < tips[lo][1] + 2 * U:
        raise IdiomError("crystal: no room under the pin rows")
    gpts = []
    # a ground pin that sits ABOVE the signal pin of its side cannot drop straight down: its wire runs on past
    # that side's drop (one square crossing) and comes down outside it
    outer = set()
    for no in sorted(po, key=str):
        o = tuple(ps[no]["out"])
        if D.is_gnd(po[no]) and o[1] == 0:
            a_same = a_up if o[0] == s else a_lo
            if ps[no]["xy"][1] < a_same[1] - 0.01:
                outer.add(o[0])
    jogged = {}
    if jog:
        for no in sorted(po, key=str):
            p = ps[no]
            o = tuple(p["out"])
            if not (D.is_gnd(po[no]) and o[1] == 0 and o[0] in outer):
                continue
            t = up if o[0] == s else lo
            a_t, xd = (a_up, xf) if t == up else (a_lo, xn)
            tip = (snap(p["xy"][0]), snap(p["xy"][1]))
            if tip[1] > a_t[1] - 0.01:
                continue
            if (xd - a_t[0]) * o[0] < 5 * 2 * U - 0.01:
                raise IdiomError("crystal: no room for the case pin's ground beside the drop")
            q = (snap(tip[0] + o[0] * 3 * 2 * U), snap(tip[1] + 2 * U))
            pb = power_box(D.text(po[no]), q, "down")
            yj = snap(max(a_t[1] + 2 * 2 * U, math.ceil((pb[3] + 0.8) / (2 * U)) * 2 * U))
            jogged[t] = (no, tip, q, yj, o[0])
        if not jogged:
            raise IdiomError("crystal: nothing to step around")
    nodes = {lo: (xn, a_lo[1]), up: (xf, a_up[1])}
    for t, a_t, xd in ((lo, a_lo, xn), (up, a_up, xf)):
        if t in jogged:
            _no, _tip, _q, yj, d = jogged[t]
            x1 = snap(a_t[0] + d * 2 * U)
            nodes[t] = (xd, yj)
            T.wires.append(("N" + t, [start[t][0], (xd, tips[t][1]), (xd, yj), (x1, yj), (x1, a_t[1]), a_t]))
        else:
            T.wires.append(("N" + t, [start[t][0], (xd, tips[t][1]), (xd, a_t[1]), a_t]))
    for t, node, left in ((lo, nodes[lo], s > 0), (up, nodes[up], s < 0)):
        if "C" + t in m.roles:
            side_out = -s if t == lo else s                # which way this node lies from the crystal
            if side_out in outer and t not in jogged:
                left = not left                            # the cap's texts go between cap and crystal
            far, _bx, _ = _hang(T, D, "C" + t, m.roles["C" + t], m.nets["N" + t], node[0], node[1], 1, lead=0,
                                slot="left" if left else "right")
            gpts.append(far)
    for no in sorted(po, key=str):
        if not D.is_gnd(po[no]):
            continue
        p = ps[no]
        tip = (snap(p["xy"][0]), snap(p["xy"][1]))
        o = tuple(p["out"])
        if o == (0, 1):
            gpts.append(tip)
        elif any(j_[0] == no for j_ in jogged.values()):
            q = next(j_[2] for j_ in jogged.values() if j_[0] == no)
            T.wires.append(("GND", [tip, (q[0], tip[1]), q]))
            T.powers.append(("GND", q, "down"))
        elif o[1] == 0 and o[0] in outer and tip[1] < (a_up if o[0] == s else a_lo)[1] - 0.01:
            a1 = (snap((xf if o[0] == s else xn) + o[0] * 2 * 2 * U), tip[1])
            T.cross = True
            if OWN_GROUND:
                # (fix round 3) ... and ends in a ground symbol of its own there: taken down to the shared bar it
                # closed a ring of wire around the whole crystal
                q = (a1[0], snap(a1[1] + 2 * U))
                T.wires.append(("GND", [tip, a1, q]))
                T.powers.append(("GND", q, "down"))
                continue
            T.wires.append(("GND", [tip, a1]))
            gpts.append(a1)
        elif o[1] == 0:
            a1 = (snap(tip[0] + o[0] * 2 * U), tip[1])
            T.wires.append(("GND", [tip, a1]))
            gpts.append(a1)
        else:
            raise IdiomError("crystal: a ground pin points up")
    if gpts:
        bottom = max(b[3] for b in sym_boxes(si)[:1])
        yg = snap(max(max(g[1] for g in gpts), bottom) + 2 * U)
        for g in gpts:
            T.wires.append(("GND", [g, (g[0], yg)]))
        xs_ = sorted({g[0] for g in gpts})
        if len(xs_) > 1:
            T.wires.append(("GND", [(xs_[0], yg), (xs_[-1], yg)]))
        xm = snap((xn + xf) / 2)
        if not xs_[0] - 0.01 <= xm <= xs_[-1] + 0.01:
            xm = xs_[0]
        T.powers.append(("GND", (xm, yg), "down"))
    return T


CC_RETURN_UP = True        # class I: a CC pull-down returns to the ground pin ABOVE it instead of across the data rows


def cc_plan(D, si, m: Match) -> dict:
    """(class I) How the pull-downs of a receptacle return to ground without crossing its other rows, from the pin
    order of the side the CC pins are on (top to bottom; a connector is never turned upside down):
      'split'  connected signal rows lie BETWEEN the two CC rows (D+ / D-): the shared drop of the classic drawing
               would cross them. The upper resistor returns UP to the wire of the ground pin above it, the lower one
               ends in a ground symbol of its own
      'up'     the two CC rows are neighbours: both return up to that ground wire; a supply pin right below the lower
               CC row (it could only rise across both CC rows) is led out under them, beyond the ground symbol, and up
    -> {} (the classic drawing) or {'mode', 'g': ground pin number, 'v': supply pin number | None}"""
    if not CC_RETURN_UP:
        return {}
    ref = si.ref
    ps = {}
    for q in si.pins_world():
        ps.setdefault(q["number"], q)
    cc = [ps[n] for n in m.extra["pins"].values() if n in ps]
    if len(cc) != 2 or tuple(cc[0]["out"]) != tuple(cc[1]["out"]) or cc[0]["out"][1] != 0 \
            or abs(cc[0]["xy"][0] - cc[1]["xy"][0]) > 0.01:
        return {}
    up, lo = sorted(cc, key=lambda q: q["xy"][1])

    def net(q):
        return D.pins_of[ref].get(q["number"])
    col = sorted((q for q in ps.values() if tuple(q["out"]) == tuple(up["out"]) and abs(q["xy"][0] - up["xy"][0]) < 0.01
                  and D.conn(net(q))), key=lambda q: q["xy"][1])
    mid = [q for q in col if up["xy"][1] + 0.01 < q["xy"][1] < lo["xy"][1] - 0.01]
    g = None
    for q in reversed([q for q in col if q["xy"][1] < up["xy"][1] - 0.01]):
        if net(q) == m.nets.get("GND"):
            g = q
            break
        if not D.is_rail(net(q)):
            break
    if g is None or (mid and any(D.is_rail(net(q)) for q in mid)):
        return {}
    if mid:
        return {"mode": "split", "g": g["number"], "v": None}
    below = [q for q in col if q["xy"][1] > lo["xy"][1] + 0.01]
    v = below[0] if below and D.is_rail(net(below[0])) and not D.is_gnd(net(below[0])) else None
    return {"mode": "up", "g": g["number"], "v": v["number"] if v is not None else None}


def cc_template(D, m: Match, tips: dict, s: int, k: int, stagger: bool, gtip=None, mode=None, vtip=None):
    """USB-C configuration pull-downs: one resistor in line on each CC pin row, both joined to ONE ground symbol.
    tips = {'0': tip, '1': tip} of the connector's CC pins (pointing s).

    With `gtip` (the tip of the ground pin above the CC rows, see cc_plan) no return crosses a row of the receptacle:

        GND ──────────────────┬───┐          the ground pin's own wire runs out over the resistors; the return of
        CC1 ───[ R ]──────────┘   ⏚          the upper one (mode 'up': of both) rises to it, the ground symbol hangs
        D+  ─────────────────────────        beyond the rise. mode 'split': the lower resistor ends in its own
        CC2 ───[ R ]───┐                     ground symbol. `vtip`: a supply pin under the CC rows, led out beyond
                       ⏚                     the ground symbol and up (net role 'VB')."""
    T = Template()
    order = sorted(tips, key=lambda t: (tips[t][1], t))
    fars, prev = [], None
    for t in order:
        px, py = tips[t]
        net = "C" + t
        xs = snap(px + s * (_label_room(D, m.nets[net]) + k) * U)
        if stagger and prev is not None:
            # (with returns up: beyond the rise of the upper resistor, so the lower one's texts stand above it)
            xs = s * max(s * xs, s * snap(prev[0] + s * (6 if gtip is not None else 2) * U))
        far, _bx, _ = _inline(T, D, "r" + t, m.roles["r" + t], m.nets[net], xs, py, s)
        if t != order[0] and not stagger:                  # rows one pin pitch apart: the lower part's texts go below
            T.parts["r" + t] = T.parts["r" + t][:3] + ("below",)
        T.wires.append((net, [(px, py), (xs, py)]))
        T.labels[net] = (snap(px + s * U), py, (s, 0))
        fars.append(far)
        prev = far
    if gtip is not None:
        gy = gtip[1]
        if gy > min(f[1] for f in fars) - 2 * U + 0.01:
            raise IdiomError("usbc: the ground pin is not above the CC rows")
        up_ = fars if mode == "up" else fars[:1]
        xb = snap(s * max(s * f[0] for f in up_) + s * 2 * U)
        xg = snap(xb + s * 2 * 2 * U)
        qg = (xg, snap(gy + GAP))
        T.wires.append(("GND", [gtip, (xg, gy), qg]))
        T.powers.append(("GND", qg, "down"))
        if stagger and len(up_) > 1:
            for f in up_:                                  # one rise each, right behind its resistor
                xr = snap(f[0] + s * 2 * U)
                T.wires.append(("GND", [f, (xr, f[1]), (xr, gy)]))
        else:
            for f in up_:
                T.wires.append(("GND", [f, (xb, f[1])]))
            T.wires.append(("GND", [(xb, max(f[1] for f in up_)), (xb, gy)]))
        if mode != "up":
            f = fars[-1]
            e = (snap(f[0] + s * 2 * U), f[1])
            q = (e[0], snap(f[1] + GAP))
            T.wires.append(("GND", [f, e, q]))
            T.powers.append(("GND", q, "down"))
        if vtip is not None:
            xv = snap(xg + s * 3 * 2 * U)
            qv = (xv, snap(gy - GAP))
            T.wires.append(("VB", [vtip, (xv, vtip[1]), qv]))
            T.powers.append(("VB", qv, "up"))
        return T
    xb = snap(s * max(s * f[0] for f in fars) + s * 2 * U)
    ylow = max(f[1] for f in fars)
    q = (xb, snap(ylow + GAP))
    for f in fars:
        T.wires.append(("GND", [f, (xb, f[1])]))
    T.wires.append(("GND", [(xb, min(f[1] for f in fars)), q]))
    T.powers.append(("GND", q, "down"))
    return T
