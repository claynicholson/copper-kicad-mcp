"""Read-only idiom matcher: netlist graph of one block -> Matches, highest priority first, every part claimed once.

Nothing here looks at coordinates. The matcher only reads `Design` (pins_of, rails, roles, units)."""
from __future__ import annotations

from ... import layout as LAY
from ..blocks import _refkey, prefix
from .model import Match

NODE_PARTS = ("R", "C", "SW", "D", "LED", "RV", "TH", "NTC")
_DOWN_ORDER = {"R": 0, "RV": 0, "TH": 0, "NTC": 0, "SW": 1, "D": 2, "LED": 2, "C": 3}
MOD_GND_MIN = 5            # an IC with more ground pins than this gets grouped ground symbols


def is_led(D, r) -> bool:
    c = D.comps.get(r, {})
    name = str(c.get("lib_id", "")).split(":")[-1].upper()
    return prefix(r) in ("D", "LED") and ("LED" in name or str(c.get("value", "")).upper().startswith("LED"))


def two_pin(D, r) -> bool:
    po = D.pins_of.get(r, {})
    return len(po) == 2 and len(set(po.values())) == 2 and len(D.units.get(r, ())) == 1


TWIN_CONTACTS = True       # package B: a part whose pins are doubled inside (4-pin tactile switch) is a two-terminal part


def node_part(D, r) -> bool:
    """A part the node templates may hang on a wire: two pins on two nets - or (package B) a part of three or four
    pins that is electrically the same thing, because its pins are doubled inside (pins 1 / 2 and 3 / 4 of a tactile
    switch): every pin connected, exactly two nets."""
    if two_pin(D, r):
        return True
    po = D.pins_of.get(r, {})
    return TWIN_CONTACTS and 3 <= len(po) <= 4 and D.npins(r) == len(po) and len(D.units.get(r, ())) == 1         and len(set(po.values())) == 2 and all(D.conn(k) for k in po.values())


def ends(D, r, near):
    """(near pin, far pin, far net) of a 2-pin part seen from net `near` (a part with doubled pins: the first pin
    of either net)."""
    po = sorted(D.pins_of[r].items())
    if len(po) != 2:
        pn = next(p for p, k in po if k == near)
        pf, M = next((p, k) for p, k in po if k != near)
        return pn, pf, M
    (p1, n1), (p2, n2) = po
    return (p1, p2, n2) if n1 == near else (p2, p1, n1)


def _cls(D, k) -> str:
    if D.is_gnd(k):
        return "down"
    if D.is_rail(k):
        return "up"
    return "series" if D.conn(k) else "open"


def decap_matches(D, info, claimed: set) -> list:
    """Capacitors between a supply rail and ground, grouped by the IC (and rail) they decouple."""
    by: dict = {}
    for r in info.sats:
        if r in claimed or prefix(r) != "C" or not two_pin(D, r):
            continue
        a = D.role.get(r, {}).get("anchor")
        if a is None or a not in info.anchors:
            continue
        (p1, n1), (p2, n2) = sorted(D.pins_of[r].items())
        for na, nb in ((n1, n2), (n2, n1)):
            if D.is_rail(na) and not D.is_gnd(na) and D.is_gnd(nb) and na in D.pins_of[a].values():
                by.setdefault((a, na, nb), []).append(r)
                break
    out = []
    for (a, rail, gnd), caps in sorted(by.items(), key=lambda t: (_refkey(t[0][0]), D.text(t[0][1]))):
        # the small caps stand closest to the pins
        caps.sort(key=lambda c: (LAY.cap_farads(D.comps[c]["value"]) or 0, _refkey(c)))
        out.append(Match("decap", {f"c{i}": c for i, c in enumerate(caps)}, {"RAIL": rail, "GND": gnd}, (a, None), 90))
        claimed.update(caps)
    return out


def node_match(D, info, N, claimed: set, host=None):
    """The 2-pin parts hanging on signal net N: pull, divider, button, RC filter, LED chain - or None when the
    constellation is not one the node templates draw."""
    if not D.conn(N) or D.is_rail(N):
        return None
    sat = [r for r in info.sats if r not in claimed and node_part(D, r) and N in D.pins_of[r].values()]
    if not sat:
        return None
    if any(prefix(r) not in NODE_PARTS for r in sat):
        return None                                      # inductors, ferrites, fuses, crystals: stage idioms
    ups, downs, series = [], [], []
    for r in sorted(sat, key=_refkey):
        _pn, _pf, M = ends(D, r, N)
        c = _cls(D, M)
        if c == "open":
            return None
        if prefix(r) in ("D", "LED") and not is_led(D, r) and c != "down":
            return None                                  # a diode towards a rail is a power path, not a clamp
        (ups if c == "up" else downs if c == "down" else series).append(r)
    if len(series) > 1 or len(ups) > 2:
        return None
    downs.sort(key=lambda r: (_DOWN_ORDER.get(prefix(r), 9), _refkey(r)))
    roles, nets = {}, {"N": N}
    for i, r in enumerate(ups):
        roles[f"up{i}"] = r
        nets[f"up{i}"] = ends(D, r, N)[2]
    for i, r in enumerate(downs):
        roles[f"down{i}"] = r
        nets[f"down{i}"] = ends(D, r, N)[2]
    kinds = {prefix(r) for r in ups + downs}
    idiom = "button" if "SW" in kinds else "divider" if (ups and any(prefix(r) != "C" for r in downs)
                                                         and all(prefix(r) in ("R", "RV", "TH", "NTC") for r in ups)) else "pull"
    if series:
        s = series[0]
        if prefix(s) == "C":
            return None                                  # AC coupling: not a node idiom
        M = ends(D, s, N)[2]
        roles["series"] = s
        nets["M"] = M
        tail = [r for r in info.sats if r not in claimed and r != s and node_part(D, r) and M in D.pins_of[r].values()
                and r not in sat]
        if any(prefix(r) not in NODE_PARTS for r in tail):
            return None
        tups, tdowns = [], []
        for r in sorted(tail, key=_refkey):
            c = _cls(D, ends(D, r, M)[2])
            if c == "open" or (prefix(r) in ("D", "LED") and not is_led(D, r) and c != "down"):
                return None
            if c == "series":
                # the net goes on through another series part: this template ends at M, the rest is drawn by
                # whatever hosts that part
                continue
            (tups if c == "up" else tdowns).append(r)
        chain = [r for r in tups + tdowns if is_led(D, r)] + ([s] if is_led(D, s) else [])
        if chain:
            # LED + series resistor between a pin and a rail: its own template (drawn in the flow direction)
            if ups or downs or len(tups + tdowns) != 1 or len(D.view.nets[M].pins) != 2:
                return None
            e = (tups + tdowns)[0]
            return Match("led", {"e1": s, "e2": e}, {"N": N, "M": M, "END": ends(D, e, M)[2]}, host, 70,
                         {"end": "up" if tups else "down"})
        if len(tups) > 1:
            return None
        tdowns.sort(key=lambda r: (_DOWN_ORDER.get(prefix(r), 9), _refkey(r)))
        for i, r in enumerate(tups):
            roles[f"tup{i}"] = r
            nets[f"tup{i}"] = ends(D, r, M)[2]
        for i, r in enumerate(tdowns):
            roles[f"tdown{i}"] = r
            nets[f"tdown{i}"] = ends(D, r, M)[2]
        if not (ups or downs or tups or tdowns):
            return None                                  # a lone series part: nothing to draw as an idiom
        if any(prefix(r) == "C" for r in tdowns + downs):
            idiom = "rc"
    return Match(idiom, roles, nets, host, {"divider": 60, "button": 55, "rc": 50}.get(idiom, 40))


def match_block(D, info, claimed=None) -> dict:
    """{'decap': {(anchor, rail): Match}, 'node': {net: Match}, 'modgnd': {anchor: Match}} for one block.
    `claimed` = parts the stage idioms (idioms/stage.py) took already."""
    claimed = set(claimed or ())
    out = {"decap": {}, "node": {}, "modgnd": {}}
    for m in decap_matches(D, info, claimed):
        out["decap"][(m.host[0], m.nets["RAIL"])] = m
    for a in info.anchors:
        gp = [p for p, k in D.pins_of[a].items() if D.is_gnd(k)]
        if len(gp) >= MOD_GND_MIN:
            out["modgnd"][a] = Match("modgnd", {}, {"GND": D.pins_of[a][gp[0]]}, (a, None), 80, {"pins": len(gp)})
    # nets on anchor pins, big anchors first; a part is claimed by the first node that takes it
    order = []
    for a in info.anchors:
        for p in sorted(D.pins_of[a], key=lambda p: _refkey(str(p))):
            k = D.pins_of[a][p]
            if D.conn(k) and not D.is_rail(k) and k not in [o[0] for o in order]:
                order.append((k, (a, p)))
    for k, host in order:
        m = node_match(D, info, k, claimed, host)
        if m is not None and m.roles:
            out["node"][k] = m
            claimed.update(m.refs())
    return out


def match_design(D) -> dict:
    """All matches of a design -> {'matches': [Match], 'parts': n, 'claimable': n}. Read-only (used by the score)."""
    from . import stage as ST
    ms = []
    for info in D.blocks:
        taken: set = set()
        ms += ST.all_matches(ST.match_block(D, info, taken))
        mb = match_block(D, info, taken)
        ms += list(mb["decap"].values()) + list(mb["node"].values()) + list(mb["modgnd"].values())
    refs = {r for m in ms for r in m.refs()}
    return {"matches": ms, "parts": len(D.units), "claimable": len(refs)}
