"""Sheet-local view of a schematic + functional-block detection.

The engine never works on raw netlist names: what a net is CALLED on this sheet (its labels, its power symbol)
comes from the sheet itself (`SheetView`), the netlist only says which pins belong together.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ... import sexpr
from .. import layout as LAY
from .. import ops as O
from ..model import Schematic

SMALL = {"R", "C", "L", "D", "LED", "FB", "F", "Y", "X", "TP", "JP", "SW", "BZ", "RV", "TH", "NTC", "VR", "Q", "S", "BT"}
FEEDER_HOME = True         # a TVS / bleeder on an input rail is drawn with the connector that feeds the rail
_KIND_ORDER = {"hierarchical": 0, "global": 1, "local": 2}
_MCU = re.compile(r"^(MCU_|RF_Module|CPU|FPGA_|CPLD_|DSP_)|STM32|ESP32|ESP8266|RP2040|RP2350|ATMEGA|ATTINY|ATSAM|NRF5|PIC1|PIC2|"
                  r"PIC3|GD32|CH32|MSP430|LPC\d|EFM32|ESP-", re.I)


def prefix(ref: str) -> str:
    return re.match(r"[A-Za-z_#]*", ref).group(0).upper()


@dataclass
class LocalNet:
    key: str                                   # netlist name (identity only - never drawn)
    pins: list = field(default_factory=list)   # [(ref, pin)] on THIS sheet
    names: list = field(default_factory=list)  # [(text, kind, shape, [nodes])] distinct, priority order
    power: list = field(default_factory=list)  # distinct power-symbol values on it
    flags: int = 0
    total_pins: int = 0

    @property
    def named(self) -> bool:
        return bool(self.names or self.power)


@dataclass
class SheetView:
    nets: dict                                 # key -> LocalNet
    pin_net: dict                              # (ref, pin) -> key
    ptypes: dict
    orphans: list                              # [{'names': [(text, kind, shape, node)], 'power': [values], 'flags': n}]
    nc: set                                    # (ref, pin) carrying a no-connect flag
    power_src: dict                            # value -> (lib_id, lib_name)
    flag_src: tuple | None


def sheet_view(sch: Schematic, nl: dict) -> SheetView:
    refs = {s.ref for s in sch.symbols(include_power=False)}
    pin_net, ptypes, nets = {}, {}, {}
    for key, nodes in nl["nets"].items():
        here = [(n["ref"], n["pin"]) for n in nodes if n["ref"] in refs]
        if not here:
            continue
        ln = nets.setdefault(key, LocalNet(key))
        ln.total_pins = len(nodes)
        for n in nodes:
            if n["ref"] in refs:
                ln.pins.append((n["ref"], n["pin"]))
                pin_net[(n["ref"], n["pin"])] = key
                ptypes[(n["ref"], n["pin"])] = n["pin_type"]
    orphans = []
    power_src, flag_src = {}, None
    for s in sch.symbols():
        if s.is_power:
            if O.is_flag(s):
                flag_src = flag_src or (s.lib_id, s.lib_name)
            else:
                power_src.setdefault(s.value, (s.lib_id, s.lib_name))
    for g in O._group_nets(sch, nl):
        labels = [(l["net"], l["kind"], str(sexpr.value(l["node"], "shape", "")) if l["kind"] != "local" else "", l["node"])
                  for l in g["labels"]]
        pvals = [s.value for s in g["power"]]
        key = g["net"]
        if key is None or (key not in nets):
            if labels or pvals or g["flags"]:
                # no pin of this sheet: a pass-through / name-only wire. Kept as its own strip.
                tgt = None
                if key is not None:
                    tgt = next((o for o in orphans if o.get("key") == key), None)
                if tgt is None:
                    tgt = {"key": key, "names": [], "power": [], "flags": 0}
                    orphans.append(tgt)
                tgt["names"] += labels
                tgt["power"] += [v for v in pvals if v not in tgt["power"]]
                tgt["flags"] += len(g["flags"])
            continue
        ln = nets[key]
        ln.flags += len(g["flags"])
        for v in pvals:
            if v not in ln.power:
                ln.power.append(v)
        for text, kind, shape, node in labels:
            ent = next((e for e in ln.names if e[:3] == (text, kind, shape)), None)
            if ent is None:
                ent = (text, kind, shape, [])
                ln.names.append(ent)
            ent[3].append(node)
    for o in orphans:
        merged = []
        for text, kind, shape, node in o["names"]:
            ent = next((e for e in merged if e[:3] == (text, kind, shape)), None)
            if ent is None:
                ent = (text, kind, shape, [])
                merged.append(ent)
            ent[3].append(node)
        o["names"] = merged
    for ln in nets.values():
        # the name drawn at pins: a local label when the net has one, else its interface name
        ln.names.sort(key=lambda e: (-_KIND_ORDER.get(e[1], 3), e[0]))
    from .gate import nc_marked, redundant_nc
    nc = set()
    pts = nc_marked(sch)
    for s in sch.symbols(include_power=False):
        for p in s.pins_world():
            if (round(p["xy"][0], 2), round(p["xy"][1], 2)) in pts and not redundant_nc(p):
                nc.add((s.ref, p["number"]))
    return SheetView(nets, pin_net, ptypes, orphans, nc, power_src, flag_src)


@dataclass
class BlockInfo:
    id: str
    kind: str
    rank: int
    title: str
    anchors: list
    sats: list
    stage: float = 0.0          # left-to-right position in the signal flow (compose() sorts columns by it)


class Design:
    """Everything block building needs, derived from the sheet view."""

    def __init__(self, sch: Schematic, nl: dict, view: SheetView | None = None, hints: dict | None = None,
                 near: dict | None = None):
        self.sch, self.nl = sch, nl
        self.view = view or sheet_view(sch, nl)
        v = self.view
        self.units: dict = {}
        for s in sch.symbols(include_power=False):
            self.units.setdefault(s.ref, []).append(s)
        for lst in self.units.values():
            lst.sort(key=lambda s: s.unit)
        self.comps = {r: {"value": lst[0].value, "lib_id": lst[0].lib_id, "desc": _desc(lst[0])}
                      for r, lst in self.units.items()}
        self.pins_of = {r: {} for r in self.units}
        for (r, p), k in v.pin_net.items():
            self.pins_of.setdefault(r, {})[p] = k
        self.ptypes = v.ptypes
        self.rails = {k for k, ln in v.nets.items() if ln.power}
        self.hints = hints or {}
        self.near = near or {}           # {ref: ref it was placed `near`} from the sch_apply batch being laid out
        self.blocks: list = []
        self.role: dict = {}
        self._detect()

    # ── naming ──
    def net(self, k):
        return self.view.nets.get(k)

    def is_gnd(self, k) -> bool:
        return k in self.rails and LAY.is_ground(self.view.nets[k].power[0])

    def is_rail(self, k) -> bool:
        return k in self.rails

    def conn(self, k) -> bool:
        ln = self.view.nets.get(k) if k is not None else None
        return ln is not None and (ln.named or len(ln.pins) >= 2)

    def named(self, k) -> bool:
        ln = self.view.nets.get(k)
        return bool(ln and ln.names) and k not in self.rails

    def text(self, k) -> str:
        """What the net is called on this sheet ('' when it has no name here)."""
        ln = self.view.nets.get(k)
        if ln is None:
            return ""
        if ln.power:
            return ln.power[0]
        return ln.names[0][0] if ln.names else ""

    def label_kind(self, k) -> str:
        ln = self.view.nets.get(k)
        return ln.names[0][1] if ln and ln.names else "local"

    def npins(self, r) -> int:
        return len(self.pins_of.get(r, {}))

    # ── where a part was drawn before ──
    def _frames(self) -> dict:
        """{anchor ref: [x0, y0, x1, y1]} of the block frames an earlier re-layout drew."""
        if getattr(self, "_frame_cache", None) is None:
            rects = {str(sexpr.value(n, "uuid", "")): n for n in self.sch.items("rectangle")}
            out = {}
            for g in self.sch.items("group"):
                if len(g) > 1 and str(g[1]).startswith("copper:block:"):
                    for u in (sexpr.find(g, "members") or [])[1:]:
                        n = rects.get(str(u))
                        if n is not None:
                            a, b = sexpr.find(n, "start"), sexpr.find(n, "end")
                            out[str(g[1])[len("copper:block:"):]] = [min(float(a[1]), float(b[1])), min(float(a[2]), float(b[2])),
                                                                    max(float(a[1]), float(b[1])), max(float(a[2]), float(b[2]))]
            self._frame_cache = out
        return self._frame_cache

    def _drawn_at(self, c: str, users: list, root=None):
        """The anchor among `users` whose block frame part `c` is drawn in (an earlier re-layout put it there), or
        None when the drawing does not say. Only frames count: where the label-style placer dropped a part is not
        evidence (on the li-ion-powerbank fixture a cap placed `near` the charger sat closer to the boost IC)."""
        if len(users) < 2:
            return None
        x, y = self.units[c][0].at[0], self.units[c][0].at[1]
        inside = [a for a, fb in self._frames().items() if fb[0] <= x <= fb[2] and fb[1] <= y <= fb[3]]
        if len(inside) != 1:
            return None
        cands = [a for a in users if a == inside[0] or (root is not None and root(a) == inside[0])]
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        if inside[0] in cands:
            return inside[0]            # the block's own IC, as on the first run (keeps a re-layout a no-op)
        ds = []
        for a in cands:
            pts = [p["xy"] for u in self.units[a] for p in u.pins_world()] or [self.units[a][0].at]
            ds.append((min(((px - x) ** 2 + (py - y) ** 2) ** 0.5 for px, py in pts), _refkey(a), a))
        return min(ds)[2]

    # ── detection ──
    def _detect(self):
        comps, pins_of, ptypes, v = self.comps, self.pins_of, self.ptypes, self.view
        rails = self.rails

        def supplyish(k):
            ln = v.nets[k]
            return k in rails or any("power_out" in ptypes.get(p, "") for p in ln.pins) or \
                LAY.is_supply(self.text(k), set()) or LAY.is_ground(self.text(k))

        def gndish(k):
            return LAY.is_ground(self.text(k)) and supplyish(k)
        sup = {k for k in v.nets if supplyish(k)}
        self.supplyish = sup

        def is_small(r):
            return prefix(r) in SMALL and self.npins(r) <= 4
        anchors = sorted((r for r in comps if not is_small(r)), key=_refkey)
        smalls = sorted((r for r in comps if is_small(r)), key=_refkey)
        net_refs = {k: {p[0] for p in ln.pins} for k, ln in v.nets.items()}

        def is_conn(r):
            return prefix(r) in ("J", "P", "CN", "CON", "USB", "X") or comps[r]["lib_id"].startswith("Connector")

        def is_reg(r):
            return comps[r]["lib_id"].startswith("Regulator") or (self.npins(r) <= 8 and any(
                "power_out" in ptypes.get((r, p), "") for p in pins_of[r]))

        def sig(r):
            return [k for k in pins_of[r].values() if k not in sup and self.conn(k)]
        self.is_conn, self.is_reg = is_conn, is_reg
        pname = {}
        for r, lst in self.units.items():
            for u in lst:
                for p in u.pins_world():
                    pname.setdefault((r, p["number"]), str(p.get("name") or ""))
        self.pin_name = pname

        def two(s):
            return len(pins_of[s]) == 2 and len(set(pins_of[s].values())) == 2

        def hop(nets, prefixes, hops):
            """nets reachable from `nets` through 2-pin parts (or a 3-pin FET) whose prefix is in `prefixes`."""
            seen = set(nets)
            front = set(nets)
            for _ in range(hops):
                nxt = set()
                for k in front:
                    for r in net_refs.get(k, ()):
                        if prefix(r) in prefixes and r in smalls_set and (two(r) or prefix(r) == "Q"):
                            for k2 in pins_of[r].values():
                                if k2 not in seen and k2 in v.nets and not (k2 in sup and gndish(k2)):
                                    nxt.add(k2)
                seen |= nxt
                front = nxt
            return seen
        smalls_set = set(smalls)
        reg_in, reg_out = {}, {}
        for a in anchors:
            if not is_reg(a):
                continue
            ins_ = {k for p, k in pins_of[a].items() if not (k in sup and gndish(k)) and self.conn(k) and
                    ("power_in" in ptypes.get((a, p), "") or _PIN_IN.match(pname.get((a, p), "")))}
            o0 = {k for p, k in pins_of[a].items() if not (k in sup and gndish(k)) and self.conn(k) and
                  ("power_out" in ptypes.get((a, p), "") or _PIN_OUT.match(pname.get((a, p), "")))} - ins_
            if not any("power_out" in ptypes.get((a, p), "") for p in pins_of[a]):
                o0 = hop(o0, {"L", "D", "FB"}, 2) - ins_
            reg_in[a], reg_out[a] = ins_, o0
        sourced = set().union(*reg_out.values()) if reg_out else set()
        fed = set().union(*reg_in.values()) if reg_in else set()
        self.reg_in, self.reg_out = reg_in, reg_out

        def power_feed(c):
            """A connector that brings the supply in: one of its nets reaches a regulator input (directly or
            through a fuse / diode / ferrite / reverse-protection FET), or it is a plain supply connector."""
            mine = {k for k in pins_of[c].values() if self.conn(k) and not (k in sup and gndish(k))}
            if len(set(sig(c))) > 6:
                return None                               # a GPIO header that also carries VBUS is I/O
            direct = {k for k in mine if k in sup and k not in sourced
                      and not any("power_out" in ptypes.get(p, "") for p in v.nets[k].pins)}
            if direct & fed:
                return sorted(direct & fed)[0]
            for k in sorted(mine - sourced):
                if (hop({k}, {"F", "D", "FB", "L", "Q"}, 2) & fed) - sourced - {k}:
                    return k
            if len(set(sig(c))) <= 2:
                # no regulator at all: the jack feeds a rail through its protection part (9 V - Schottky - "+9V")
                for k in sorted(mine - sourced - set(rails)):
                    for r in sorted(net_refs.get(k, ()), key=_refkey):
                        if r not in smalls_set or prefix(r) not in ("F", "D", "FB", "L") or not two(r):
                            continue
                        if "LED" in (comps[r]["lib_id"] + " " + comps[r]["value"]).upper():
                            continue
                        k2 = next((x for x in pins_of[r].values() if x != k), None)
                        if k2 in rails and k2 not in sourced and not gndish(k2) and not any(
                                "power_out" in ptypes.get(p, "") for p in v.nets[k2].pins):
                            return k
            plain = sorted(k for k in direct if k in rails)
            if plain and len(set(sig(c))) <= 4 and not any(k in sourced for k in mine if k in sup):
                return plain[0]
            return None
        feed_of = {}
        for c in anchors:
            if is_conn(c):
                k = power_feed(c)
                if k is not None:
                    feed_of[c] = k
        parent: dict = {}
        for a in anchors:
            s = set(sig(a))
            if is_conn(a):                                # driver outputs are 'power_out' pins: still its signals
                s = {k for k in pins_of[a].values() if self.conn(k) and k not in rails and not gndish(k)}
            if not s or self.npins(a) > 8 or is_reg(a):
                continue
            if is_conn(a):
                # a small connector that talks to ONE part (motor terminals at their driver, the debug header at
                # the MCU, the sensor input at its amplifier) is drawn at that part's pins, wired - a frame of its
                # own would hold a lone symbol and a column of labels. Supply inputs / outputs keep their block.
                gives_ = any(k in sourced and k in rails and not gndish(k) for k in pins_of[a].values()) and len(s) <= 1
                if a in feed_of or self.npins(a) > 6 or a in self.hints or gives_:
                    continue
            if comps[a]["lib_id"].split(":")[0].startswith("Sensor") and a not in self.hints:
                continue                                  # a sensor is a function of its own, not MCU support
            s = {k for k in s if net_refs[k] - {a}} or s     # a net between its own pins says nothing
            cands = [b for b in anchors if b != a and b not in parent and all(b in net_refs[k] for k in s)
                     and not (is_conn(a) and (is_conn(b) or is_reg(b)))]
            if cands:
                cands.sort(key=lambda b: (not is_conn(b), -self.npins(b), _refkey(b)))
                parent[a] = cands[0]

        def root(a):
            seen = set()
            while a in parent and a not in seen:
                seen.add(a)
                a = parent[a]
            return a
        role: dict = {}
        hint_anchor = {}
        for r, h in self.hints.items():
            if h in comps and r in comps:
                hint_anchor[r] = h
        for s in smalls:
            if s in hint_anchor and hint_anchor[s] in anchors:
                nets_s = sig(s) or list(pins_of[s].values())
                via = next((k for k in nets_s if hint_anchor[s] in net_refs.get(k, ())), nets_s[0] if nets_s else None)
                role[s] = {"anchor": hint_anchor[s], "via": via}
                continue
            best = None
            for k in sig(s):
                for a in anchors:
                    if a in net_refs[k]:
                        shared = sum(1 for k2 in set(sig(s)) if a in net_refs[k2])
                        key = (-shared, is_conn(a) and len(sig(s)) > 1, len(net_refs[k]), -self.npins(a), _refkey(a))
                        if best is None or key < best[0]:
                            best = (key, a, k)
            if best:
                role[s] = {"anchor": best[1], "via": best[2]}
        def chains():
            changed = True
            while changed:
                changed = False
                for s in smalls:
                    if s in role:
                        continue
                    for k in sig(s):
                        peers = sorted((p for p in net_refs[k] if p in role), key=_refkey)
                        if peers:
                            role[s] = {"anchor": role[peers[0]]["anchor"], "via": k, "chain": peers[0]}
                            changed = True
                            break
        chains()
        # the supply path: fuse / diode / ferrite / FET between the input connector and the rail it feeds
        for c, k0 in sorted(feed_of.items(), key=lambda t: _refkey(t[0])):
            front, seen = [k0], {k0}
            for _ in range(2):
                nxt = []
                for k in front:
                    if k in fed and k != k0:
                        continue                          # arrived at the regulator: what hangs there is its own
                    for s in sorted(net_refs.get(k, ()), key=_refkey):
                        if s not in smalls_set or prefix(s) not in ("F", "D", "FB", "L", "Q") or s in hint_anchor:
                            continue
                        nets_s = set(pins_of[s].values())
                        others = [k2 for k2 in nets_s if k2 != k and not (k2 in sup and gndish(k2))]
                        if not others or (nets_s & sourced) or (role.get(s, {}).get("anchor") == c):
                            continue
                        if s in role and not (is_reg(role[s]["anchor"]) or is_conn(role[s]["anchor"])):
                            continue
                        role[s] = {"anchor": c, "via": k, "kind0": "path"}
                        for k2 in others:
                            if k2 not in seen:
                                seen.add(k2)
                                nxt.append(k2)
                front = nxt
        chains()
        by_rail: dict = {}
        for s in smalls:
            if s in role or any(p in role for k in sig(s) for p in net_refs[k]):
                continue
            rs = [k for k in pins_of[s].values() if k in sup and not gndish(k)]
            if rs:
                by_rail.setdefault(rs[0], []).append(s)

        def farads(c):
            return LAY.cap_farads(comps[c]["value"]) or 0
        for rail in sorted(by_rail):
            caps = by_rail[rail]
            users = [a for a in anchors if a in net_refs[rail]]
            srcs = [a for a in users if is_reg(a) and any(
                pins_of[a][p] == rail and "power_out" in ptypes.get((a, p), "") for p in pins_of[a])]
            ins = [a for a in users if is_reg(a) and a not in srcs]
            cons = [a for a in users if not is_reg(a) and not is_conn(a) and a not in parent] or \
                   [a for a in users if a not in srcs and a not in ins]
            cons.sort(key=lambda a: (-self.npins(a), _refkey(a)))
            cons = [a for a in cons for _ in range(max(1, sum(1 for p in pins_of[a] if pins_of[a][p] == rail)))]
            # only capacitors are decoupling: a TVS / bleeder / indicator on the rail belongs where the rail
            # comes from (its regulator, or the connector that brings it in), not at some consumer
            # (fix round 7) ... the connector that brings it in also when a fuse / diode / ferrite stands between the
            # two (the TVS behind the fuse of a 12 V input is the input's protection, not a part of the buck it feeds)
            feeders = [c for c in sorted(feed_of, key=_refkey)
                       if FEEDER_HOME and rail in hop({feed_of[c]}, {"F", "D", "FB", "L", "Q"}, 2)]
            homes = srcs or [a for a in users if rail in reg_out.get(a, ())] or feeders or ins or                 [c for c in users if c in feed_of] or [c for c in users if is_conn(c)]
            for c in [c for c in caps if prefix(c) != "C"]:
                if homes and self.near.get(c) not in users:
                    role[c] = {"anchor": homes[0], "via": rail}
                    caps.remove(c)
            caps.sort(key=lambda c: (farads(c), _refkey(c)))
            # a cap that was already drawn at one of the rail's users stays with it: sch_apply's `near` (the
            # agent said whose decoupling cap this is) and an earlier re-layout both survive
            todo = []
            for c in caps:
                a = self.near.get(c) if self.near.get(c) in users else self._drawn_at(c, users, root)
                if a is not None:
                    role[c] = {"anchor": a, "via": rail}
                    if a in srcs:
                        srcs = []                        # the source has its output cap
                else:
                    todo.append(c)
            if srcs and todo:
                role[todo.pop()] = {"anchor": srcs[0], "via": rail}
            while ins and todo and farads(todo[-1]) > 1.01e-6 and (not srcs or not cons):
                role[todo.pop()] = {"anchor": ins[0], "via": rail}
                if cons:
                    break
            targets = cons or ins or srcs or users
            i = 0
            for c in todo:
                if targets:
                    role[c] = {"anchor": targets[i % len(targets)] if farads(c) <= 1.01e-6 else targets[0], "via": rail}
                    i += 1
        # a part that only touches supply nets (a GND test point, a rail jumper) is no block of its own: it sits
        # where the supply is made
        regs = [a for a in anchors if is_reg(a)] or sorted(feed_of, key=_refkey)
        for s in smalls:
            if s in role or s in hint_anchor:
                continue
            ks = [k for k in pins_of[s].values() if k in v.nets]
            if ks and all(k in sup for k in ks) and regs:
                home = next((a for a in regs if any(k in pins_of[a].values() and not gndish(k) for k in ks)), regs[0])
                role[s] = {"anchor": home, "via": ks[0]}
        chains()                 # parts hanging on a part that was only attached through its supply rail
        for s, r in role.items():
            other = [k for k in pins_of[s].values() if k != r["via"]]
            o = other[0] if other else None
            if r.pop("kind0", None) == "path":
                r["kind"] = "series"
            elif r["via"] in sup:
                r["kind"] = "decap"
            elif o in sup and gndish(o):
                r["kind"] = "pulldown"
            elif o in sup:
                r["kind"] = "pullup"
            else:
                r["kind"] = "chain" if r.get("chain") else "series"
        blocks: dict = {}
        for a in anchors:
            blocks.setdefault(root(a), {"anchors": [], "sats": []})["anchors"].append(a)
        for s in smalls:
            if s in role:
                blocks[root(role[s]["anchor"])]["sats"].append(s)
            else:
                blocks.setdefault("_misc", {"anchors": [], "sats": []})["sats"].append(s)
        def is_mcu(a):
            return bool(_MCU.search(comps[a]["lib_id"] + " " + comps[a]["value"]))
        main = max(anchors, key=lambda a: (not is_conn(a) and not is_reg(a), is_mcu(a), self.npins(a), a)) if anchors else None
        if main is not None and (is_conn(main) or is_reg(main)):
            main = None
        out = []
        for key, b in blocks.items():
            val = comps[key]["value"] if key in comps else ""
            if key == "_misc":
                tp = all(prefix(x) == "TP" for x in b["sats"])
                kind, title, rank = "misc", "TEST POINTS" if tp else "MISC", 9
            elif is_reg(key):
                ins_ = sorted({self.text(k) for k in reg_in.get(key, ())} - {""})
                outs_ = sorted({self.text(k) for k in reg_out.get(key, ()) if k in rails} - {""}) or \
                    sorted({self.text(k) for k in reg_out.get(key, ())} - {""})
                kind, rank = "power", 1
                what = _reg_kind(comps[key])
                title = f"{what}  {' / '.join(ins_)} → {' / '.join(outs_)}" if ins_ and outs_ else f"{what}  {_short(val)}"
            elif is_conn(key):
                feeds = key in feed_of
                kind, rank = ("input", 0) if feeds else ("io", 4)
                gives = not feeds and any(k in sourced and k in rails and not gndish(k) for k in pins_of[key].values()) \
                    and len(set(sig(key))) <= 3
                title = _conn_title(key, comps[key], [self.text(k) for k in pins_of[key].values()], feeds, gives)
            elif key == main:
                kind, rank = "main", 2
                title = ("MCU  " if is_mcu(key) else _func(comps[key])) + _short(val)
            else:
                kind, rank, title = "ic", 3, _func(comps[key]) + _short(val)
            out.append(BlockInfo(key, kind, rank, title.strip(), sorted(b["anchors"], key=lambda a: (a != key, _refkey(a))),
                                 sorted(b["sats"], key=_refkey)))
        out.sort(key=lambda b: (b.rank, _refkey(b.id)))
        self._flow(out, sup)
        self.blocks, self.role, self.root_of = out, role, {a: root(a) for a in anchors}
        # exposed for sch/pro/flow.py: which connector brings the supply in (and on which net), which nets the
        # regulators make, which connectors are drawn at the part they serve
        self.feed_of, self.sourced = dict(feed_of), set(sourced)
        self.docked = {a: root(a) for a in anchors if is_conn(a) and root(a) != a}
        self.block_of = {}
        for b in out:
            for r in b.anchors + b.sats:
                self.block_of[r] = b.id


    def _flow(self, out: list, sup: set):
        """BlockInfo.stage: where a block sits in the left-to-right signal flow. Supply comes in on the left
        (input connectors, regulators), then signal sources, then the ICs ordered by who drives whom (pin types:
        output -> input), then the connectors the design talks through."""
        v, pins_of, ptypes = self.view, self.pins_of, self.ptypes

        def drv(pin):
            t = ptypes.get(pin, "")
            return 2 if ("output" in t or "open_" in t or "tri_state" in t) else 0 if t == "input" else 1
        flow: dict = {}
        anchors_of = {a: b.id for b in out for a in b.anchors}
        net_refs = {k: {p[0] for p in ln.pins} for k, ln in v.nets.items()}
        for k in sorted(v.nets):
            ln = v.nets[k]
            if k in sup or not self.conn(k):
                continue
            ext = [(p, 0) for p in ln.pins if p[0] in anchors_of]
            for r in sorted(net_refs[k]):                  # one hop through a series R / C / ferrite
                if r not in anchors_of and len(pins_of[r]) == 2 and prefix(r) in ("R", "C", "FB", "L"):
                    for k2 in pins_of[r].values():
                        if k2 != k and k2 not in sup and k2 in v.nets:
                            ext += [(p, 1) for p in v.nets[k2].pins if p[0] in anchors_of]
            for i in range(len(ext)):
                for j in range(i + 1, len(ext)):
                    (pa, ha), (pb_, hb) = ext[i], ext[j]
                    A, B = anchors_of[pa[0]], anchors_of[pb_[0]]
                    if A == B or (ha and hb):
                        continue
                    ca, cb = self.is_conn(pa[0]), self.is_conn(pb_[0])
                    if ca and not cb:
                        d = {0: 1, 2: -1}.get(drv(pb_), 0)
                    elif cb and not ca:
                        d = {0: -1, 2: 1}.get(drv(pa), 0)
                    else:
                        d = drv(pa) - drv(pb_)
                    if d:
                        key = (A, B) if d > 0 else (B, A)
                        flow[key] = flow.get(key, 0) + abs(d)
        self.flow = flow
        ics = [b for b in out if b.kind in ("main", "ic")]
        net_f = {}
        for a in ics:
            for b in ics:
                if a.id < b.id:
                    f = flow.get((a.id, b.id), 0) - flow.get((b.id, a.id), 0)
                    if f:
                        net_f[(a.id, b.id) if f > 0 else (b.id, a.id)] = abs(f)
        layer = {b.id: 0 for b in ics}
        for _ in range(len(ics)):                          # longest path; a cycle simply stops growing
            ch = False
            for (a, b), f in sorted(net_f.items()):
                if layer[b] < layer[a] + 1 and layer[a] + 1 < len(ics):
                    layer[b] = layer[a] + 1
                    ch = True
            if not ch:
                break
        for b in out:
            if b.kind == "input":
                b.stage = 0.0
            elif b.kind == "power":
                b.stage = 1.0
            elif b.kind in ("main", "ic"):
                b.stage = 3.0 + layer[b.id] + (0.0 if net_f else 0.5 * (b.kind == "ic"))
            elif b.kind == "io":
                fwd = sum(f for (a, c), f in flow.items() if a == b.id)
                back = sum(f for (a, c), f in flow.items() if c == b.id)
                b.stage = 2.0 if (fwd > 0 and back == 0) else 90.0
            else:
                b.stage = 91.0


_PIN_IN = re.compile(r"^(VIN|IN|VI|VCC|VDD|VBAT|VBUS|V\+|VS|PVIN|AVIN)\d*$", re.I)
_PIN_OUT = re.compile(r"^(VOUT|OUT|VO|SW|LX|OUTPUT)\d*$", re.I)
_FUNC = (("Amplifier", "AMPLIFIER"), ("Analog_ADC", "ADC"), ("Analog_DAC", "DAC"), ("Sensor", "SENSOR"),
         ("Driver_Motor", "MOTOR DRIVER"), ("Driver", "DRIVER"), ("Memory", "MEMORY"), ("Battery_Management", "BATTERY"),
         ("Interface_USB", "USB"), ("Interface_UART", "UART"), ("Interface_CAN", "CAN"), ("Interface", "INTERFACE"),
         ("Power_Protection", "PROTECTION"), ("Reference_Voltage", "REFERENCE"), ("Timer", "TIMER"),
         ("Comparator", "COMPARATOR"), ("RF", "RADIO"), ("Display", "DISPLAY"), ("Audio", "AUDIO"),
         ("Isolator", "ISOLATOR"), ("Transistor", "SWITCH"), ("Relay", "RELAY"), ("Oscillator", "OSCILLATOR"))


_WORDS = ((r"charg", "BATTERY CHARGER"), (r"protection ic|battery protect", "BATTERY PROTECTION"),
          (r"fuel ?gauge", "FUEL GAUGE"), (r"\bflash\b|\d+\s?mbit|eeprom", "MEMORY"), (r"h-bridge|motor driver", "MOTOR DRIVER"),
          (r"level (shift|translat)", "LEVEL SHIFTER"), (r"usb.{0,12}(uart|serial)|uart.{0,12}usb", "USB-UART"),
          (r"\besd\b|tvs", "ESD PROTECTION"), (r"op(erational)?[ -]?amp", "AMPLIFIER"), (r"a/d converter|\badc\b", "ADC"),
          (r"n-channel|p-channel|mosfet", "SWITCH"))


_BUS = ((r"rs-?485|rs-?422", "RS-485"), (r"rs-?232", "RS-232"), (r"can", "CAN"), (r"lin", "LIN"))


def _desc(sym) -> str:
    for p in sexpr.find_all(sym.node, "property"):
        if len(p) > 2 and str(p[1]) == "Description":
            return str(p[2])
    return ""


def _func(comp) -> str:
    """What an IC is for, as the first word(s) of its block title ('' when nothing says)."""
    lib = str(comp["lib_id"]).split(":")[0]
    if lib.startswith("Interface"):
        # the library folder says "UART" for every line transceiver; the part's own description says which bus
        blob = (str(comp.get("desc", "")) + " " + str(comp.get("value", ""))).lower()
        for pat, t in _BUS:
            if re.search(pat, blob):
                return t + "  "
    for k, t in _FUNC:
        if lib.startswith(k) and k != "Battery_Management":
            return t + "  "
    blob = (str(comp.get("desc", "")) + " " + str(comp["lib_id"])).lower()
    for pat, t in _WORDS:
        if re.search(pat, blob):
            return t + "  "
    return "BATTERY  " if lib.startswith("Battery_Management") else ""


def _reg_kind(comp) -> str:
    blob = (str(comp.get("desc", "")) + " " + str(comp["lib_id"]) + " " + str(comp["value"])).lower()
    if "regulator_linear" in blob or re.search(r"\bldo\b|linear", blob):
        return "LDO"
    if re.search(r"boost|step-?up", blob):
        return "BOOST"
    if re.search(r"buck|step-?down", blob):
        return "BUCK"
    return "POWER"


def _short(val: str) -> str:
    return " ".join(str(val).split())[:28]


def _wordy(val: str) -> bool:
    """A value a person typed as a name (SENSOR, MOTOR A, VIN 12V) rather than an order code."""
    v = str(val).strip()
    return bool(v) and len(v) <= 14 and not re.search(r"\d{3}|[*()/]|\d-\d|-\w+-|\dx\d|\dP", v) \
        and bool(re.search(r"[A-Za-z]{2}", v))


def _conn_title(ref: str, comp: dict, nets: list, feeds: bool, gives: bool = False) -> str:
    val = str(comp["value"])
    blob = (val + " " + comp["lib_id"]).upper()
    ns = {n.upper().lstrip("/") for n in nets if n}
    if "USB" in blob or "TYPE-C" in blob or "TYPE_C" in blob or any(n.startswith("USB_") or n in ("CC1", "CC2") for n in ns):
        c = "TYPE-C" in blob or "TYPE_C" in blob or "USB_C" in blob or bool(ns & {"CC", "CC1", "CC2"})
        return f"{'USB-C' if c else 'USB'}  {'POWER INPUT' if feeds else 'POWER OUTPUT' if gives else 'CONNECTOR'}"
    what = ""
    if any(n.startswith("SWD") or n in ("SWCLK", "SWDIO", "TCK", "TMS", "TDI", "TDO") for n in ns):
        what = "DEBUG"
    elif any(re.match(r"(UART\d?_?)?(TX|RX)D?\d*$", n) for n in ns) and len(ns) <= 6:
        what = "UART"
    elif any("SDA" in n for n in ns) and any("SCL" in n for n in ns) and len(ns) <= 6:
        what = "I2C"
    elif any("MISO" in n or "MOSI" in n or n.endswith("SCK") for n in ns) and len(ns) <= 8:
        what = "SPI"
    elif any("BAT" in n for n in ns) and len(ns) <= 3:
        what = "BATTERY"
    elif sum(1 for n in ns if re.match(r"(GP)?IO_?\d+$|P[A-K]\d+$|D\d+$|A\d+$", n)) >= 3:
        what = "GPIO HEADER"
    name = val if _wordy(val) else what
    if feeds:
        return "POWER INPUT  " + (name or ref)
    return (name if name == "GPIO HEADER" else "CONNECTOR  " + name) if name else f"CONNECTOR  {ref}"


def _refkey(r: str):
    m = re.match(r"([A-Za-z#_]*)(\d*)(.*)", str(r))
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, m.group(3))
