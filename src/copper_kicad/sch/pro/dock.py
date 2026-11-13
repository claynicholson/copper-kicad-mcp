"""pro2 block building: the anchor IC is the trunk, everything else is docked on the row (or column) of the pin
it serves and joined by ONE straight wire (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md 4.3).

  * `dock_rows`    a rigid cell is slid out along the pin's direction to the first depth where it is free and the
                   straight corridor from the pin's wire end to the cell's port is free; the wire is emitted here
                   (net-keyed, on a corridor nothing else touches), so no router search and no dogleg is involved
  * `pack_1d`      1-D packing of cells along one side in pin order (least total displacement from the pin rows).
                   NOT used by `build_block` yet: packing the cells that found no straight line and attaching them
                   afterwards was measured and made stm32-sensor worse (wired 0.96 -> 0.76, one overlap), because
                   later parts dock on earlier ones. Docking is therefore sequential in pin order with staggered
                   depth (`dock_rows`); the legacy slide search (`Cell.attach`) is the per-part fallback
  * stage idioms   op-amp loop, LDO, crystal, CC pull-downs (templates proven by idioms.model.instantiate), the
                   power line of a boost / buck converter and peer ICs / connectors facing their host
  * `build_block`  replaces the legacy `Blk.build` slide search for "pro2"; `Blk.build_legacy` stays the fallback
                   (per part: `attach`; per block: the engine rebuilds the block the legacy way if this raises)
"""
from __future__ import annotations

import math

from ..model import text_width
from .blocks import _refkey
from .build import GAP as GAP_
from .build import STUB, U, Blk, hit, lbl_box, pins, power_boxes, set_pose, snap, sym_boxes, wbox
from . import bundle as BUNDLE
from . import route as ROUTE
from .idioms import stage as ST
from .idioms import model as IMOD
from .idioms.model import IdiomError

NODE_ON_WIRE = True        # package B: pull / filter parts that found no room at the pin hang on the pin's WIRE further out
RISER_ROWS = (4, 5, 6, 7, 8, 10, 12)   # ... or on a branch that leaves the wire so many pin rows up / down (one bend)
TAP = True                 # fix round 4: a connector whose only signal is taken at the host pin gets a branch of its own
CRYSTAL_BEST = True        # class I: the crystal drawing with the fewest crossings / shortest wires, not the first that fits
BUS_WHOLE = True           # fix round 3: a bus between two facing parts is wired completely or named completely
DIRS = {"L": (-1, 0), "R": (1, 0), "T": (0, -1), "B": (0, 1)}
SIDE = {v: k for k, v in DIRS.items()}


# ───────────────────────── 1-D packing ─────────────────────────
def pack_1d(want: list, lo: list, hi: list, gap: float = 0.0) -> list:
    """Positions for items that must keep their order along one axis without overlapping: item i wants to sit at
    want[i] and occupies [x + lo[i], x + hi[i]]. Least total squared displacement (pool-adjacent-violators on the
    shifted problem), so an item that fits on its own row stays exactly there."""
    n = len(want)
    if n == 0:
        return []
    off = [0.0] * n                                         # x[i] >= x[i-1] + (hi[i-1] - lo[i] + gap)
    for i in range(1, n):
        off[i] = off[i - 1] + (hi[i - 1] - lo[i] + gap)
    blocks = []                                             # [sum of targets, count]
    for i in range(n):
        blocks.append([want[i] - off[i], 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1] - 1e-9:
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out = []
    for s, c in blocks:
        out += [s / c] * c
    return [out[i] + off[i] for i in range(n)]


# ───────────────────────── docking primitive ─────────────────────────
def _label_steps(D, net) -> int:
    return max(2, 2 * math.ceil((text_width(D.text(net)) + 3.0) / (2 * U))) if D.named(net) else 2


def dock_rows(B: Blk, sub, pairs: list, out, kmin=None, kmax: int = 56, clr: float = 1.0, need: int = 1) -> int:
    """Dock `sub` so that every pair (net, open wire end of the host, port of the sub) lies on ONE line along `out`
    (the pairs must already agree on the offset across that line). The first depth at which the cell is clear of
    everything and all corridors are free wins; else the depth with the most free corridors (at least `need`).
    The straight wires of the free corridors are emitted. -> number of straight wires (0 = not docked)."""
    D = B.D
    if not pairs:
        return 0
    out = tuple(out)
    nets = {p[0] for p in pairs}
    if kmin is None:
        kmin = max(_label_steps(D, k) for k in nets) if out[1] == 0 else 2
    host = B.boxes + [b for n, b in B.resv if n not in nets]
    sboxes = sub.boxes + [b for n, b in sub.resv if n not in nets]
    _net0, tip0, port0 = pairs[0]
    best = None
    # a row may cross ONE vertical wire of another net squarely (the ground drop of an in-line pull-down beside a
    # USB-C receptacle's CC pin): 2.54 mm or more from that wire's ends, nothing else within 2.54 mm of the crossing
    vw = {}
    if out[1] == 0 and B.v2:
        for n2, a, b in B.wires:
            if abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) > 0.01:
                vw[tuple(round(v, 3) for v in wbox(a, b))] = (n2, a, b)

    def crossable(hits, net, y) -> bool:
        if len(hits) != 1:
            return False
        w = vw.get(tuple(round(v, 3) for v in hits[0]))
        if w is None or w[0] == net:
            return False
        x = w[1][0]
        if not (min(w[1][1], w[2][1]) + 2 * U - 0.01 <= y <= max(w[1][1], w[2][1]) - 2 * U + 0.01):
            return False
        for _n3, a3, b3 in B.wires:
            if any(max(abs(e[0] - x), abs(e[1] - y)) < 2.53 for e in (a3, b3)):
                return False
        return not any(max(abs(t[1][0] - x), abs(t[1][1] - y)) < 2.53 for t in B.terms)
    for k in range(kmin, kmax, 2):
        dx = snap(tip0[0] + out[0] * k * U - port0[0])
        dy = snap(tip0[1] + out[1] * k * U - port0[1])
        moved = [[b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy] for b in sboxes]
        if any(hit([m[0] - clr, m[1] - clr, m[2] + clr, m[3] + clr], o, 0) for m in moved for o in host):
            continue
        free = []
        for net, tip, port in pairs:
            tgt = (snap(port[0] + dx), snap(port[1] + dy))
            if (out[0] and abs(tgt[1] - tip[1]) > 0.01) or (out[1] and abs(tgt[0] - tip[0]) > 0.01):
                continue                                   # not on this pin's line
            if (tgt[0] - tip[0]) * out[0] + (tgt[1] - tip[1]) * out[1] < 2 * U - 0.01:
                continue
            a0 = (tip[0] + out[0] * 0.6, tip[1] + out[1] * 0.6)
            a1 = (tgt[0] - out[0] * 0.6, tgt[1] - out[1] * 0.6)
            cor = wbox(a0, a1, 0.3)
            hits = [o for o in host if hit(cor, o, 0)]
            if any(hit(cor, m, 0) for m in moved) or (hits and not crossable(hits, net, tip[1])):
                continue
            free.append((net, tip, tgt))
        if len(free) == len(pairs):
            best = (k, dx, dy, free)
            break
        if len(free) >= need and (best is None or len(free) > len(best[3])):
            best = (k, dx, dy, free)
    if best is None:
        return 0
    _k, dx, dy, free = best
    sub.shift(dx, dy)
    ends_ = {tip for _n, tip, _t in free} | {tgt for _n, _t, tgt in free}

    def keep(nb):
        n, b = nb
        return n not in nets or not any(b[0] - 0.4 <= p[0] <= b[2] + 0.4 and b[1] - 0.4 <= p[1] <= b[3] + 0.4 for p in ends_)
    B.resv = [nb for nb in B.resv if keep(nb)]             # the names move onto the new wires
    sub.resv = [nb for nb in sub.resv if keep(nb)]
    B.absorb(sub)
    for net, tip, tgt in free:
        B.wires.append((net, tip, tgt))
        B.boxes.append(wbox(tip, tgt))
    return len(free)


def dock_port(B: Blk, sub, net, tip, out, port=None, **kw) -> bool:
    out = tuple(out)
    if port is None:
        port = next((t[1] for t in sub.terms if t[0] == net and tuple(t[2]) == (-out[0], -out[1])), None)
    if port is None:
        return False
    return dock_rows(B, sub, [(net, tip, port)], out, **kw) > 0


def _pin(B, ref, no):
    for s in B.syms:
        if s.ref == ref:
            p = pins(s).get(no)
            if p is not None:
                return p
    return None


def _tip(p):
    return (snap(p["xy"][0]), snap(p["xy"][1]))


def _drop(B: Blk, m, why: str):
    """A stage match that could not be drawn: its parts go back to the ordinary path."""
    B.stats["dropped"].append((m.idiom, why))
    placed = {s.ref for s in B.syms}
    B.used.difference_update(r for r in m.refs() if r not in placed)


# ───────────────────────── stage idioms at the trunk ─────────────────────────
def _stage_skip(B: Blk, ref, ui) -> set:
    """Pins of this unit that a crystal / CC template will draw from the pin tip."""
    skip = set()
    here = {p["number"] for p in B.D.units[ref][ui].pins_world()}
    for m in B.sm["crystal"]:
        if m.host[0] == ref and not m.extra.get("done"):
            skip |= set(m.extra["pins"].values()) & here
    m = B.sm["usbc"].get(ref)
    if m is not None:
        skip |= set(m.extra["pins"].values()) & here
        if set(m.extra["pins"].values()) <= here and not m.extra.get("done"):
            # (class I) the ground pin the pull-downs return to, the supply pin that is led out under them
            plan = ST.cc_plan(B.D, B.D.units[ref][ui], m)
            m.extra["plan"] = plan
            skip |= {plan.get(t) for t in ("g", "v") if plan.get(t) is not None}
    m = B.sm["power"].get(ref)
    if m is not None and m.extra.get("sense") in here and not m.extra.get("done"):
        skip.add(m.extra["sense"])          # the feedback pin on the output rail: wired by `sense_wire`
    return skip


def place_unit(B: Blk, ref, ui, at, face=None):
    """Pose one unit and draw what hangs on its pins: stage idioms first, then the node idioms of `Blk.place`."""
    D = B.D
    si = D.units[ref][ui]
    skip = _stage_skip(B, ref, ui)
    m = B.sm["opamp"].get((ref, ui))
    if m is not None and place_opamp(B, si, m, at, skip):
        pass
    elif ui == 0 and ref in B.sm["ldo"] and place_ldo(B, si, B.sm["ldo"][ref], at, skip):
        pass
    else:
        def hook():
            for mc in B.sm["crystal"]:
                if mc.host[0] == ref and set(mc.extra["pins"].values()) <= skip and not mc.extra.get("done"):
                    mc.extra["done"] = True
                    try_crystal(B, mc)
            mu = B.sm["usbc"].get(ref)
            if mu is not None and set(mu.extra["pins"].values()) <= skip and not mu.extra.get("done"):
                mu.extra["done"] = True
                try_cc(B, mu)
        B.place(si, at, face=face, skip=skip, hook=hook if skip else None)


def place_opamp(B: Blk, si, m, at, skip=()) -> bool:
    D = B.D
    e = m.extra
    pose = ST.pose_lr(si, [e["inv"], e["ni"]], [e["out"]], upper=e["inv"], lower=e["ni"])
    if pose is None:
        _drop(B, m, "no pose with the inverting input on top")
        return False
    B.place(si, at, rot=pose[0], mirror=pose[1], skip={e["out"], e["inv"]} | set(skip))
    ps = pins(si)
    top = min(b[1] for b in sym_boxes(si))
    ctx = B._ctx_pins()
    why = "no room for the feedback loop"
    for k in (4, 6, 8, 10, 12, 16, 20):
        try:
            piece = IMOD.instantiate(D, m, ST.opamp_template(D, m, _tip(ps[e["out"]]), _tip(ps[e["inv"]]), k, top), ctx)
        except IdiomError as ex:
            why = str(ex)
            continue
        if B._fits(piece):
            B._commit(piece, m)
            B.stats["claimed"].add(si.ref)
            return True
    _drop(B, m, why)
    for no in (e["out"], e["inv"]):
        B.plain_stub(ps[no], D.pins_of[si.ref].get(no))
    return True


def place_ldo(B: Blk, si, m, at, skip=()) -> bool:
    D = B.D
    e = m.extra
    pose = ST.pose_lr(si, [e["in"]], [e["out"]])
    if pose is None:
        _drop(B, m, "IN and OUT are not on opposite sides")
        return False
    own = {p for p, k in D.pins_of[si.ref].items() if k in (m.nets["IN"], m.nets["OUT"])
           and _tip(pins(si).get(p, {"xy": (1e9, 1e9)})) in ()}
    stacked = {no for no, p in pins(si).items() if D.pins_of[si.ref].get(no) == m.nets["OUT"]}
    skip_ = {e["in"]} | stacked | own | set(skip)
    B.place(si, at, rot=pose[0], mirror=pose[1], skip=skip_)
    ps = pins(si)
    tips_out = {_tip(ps[no]) for no in stacked}
    ctx = B._ctx_pins()
    why = "no room at the pins"
    if len(tips_out) == 1:
        for k in (4, 6, 8, 10, 12):
            try:
                piece = IMOD.instantiate(D, m, ST.ldo_template(D, m, _tip(ps[e["in"]]), _tip(ps[e["out"]]), k), ctx)
            except IdiomError as ex:
                why = str(ex)
                continue
            if B._fits(piece):
                B._commit(piece, m)
                B.stats["claimed"].add(si.ref)
                return True
    _drop(B, m, why)
    seen = set()
    for no in sorted({e["in"]} | stacked, key=str):
        if _tip(ps[no]) not in seen:
            seen.add(_tip(ps[no]))
            B.plain_stub(ps[no], D.pins_of[si.ref].get(no))
    return True


def _host_tips(B: Blk, m, tags) -> tuple:
    a = m.host[0]
    tips, outs = {}, set()
    for t in tags:
        p = _pin(B, a, m.extra["pins"][t])
        if p is None:
            return None, None
        tips[t] = _tip(p)
        outs.add(tuple(p["out"]))
    return tips, outs


def _give_back(B: Blk, m, tags):
    a = m.host[0]
    for t in tags:
        p = _pin(B, a, m.extra["pins"][t])
        if p is not None:
            B.plain_stub(p, B.D.pins_of[a].get(m.extra["pins"][t]))


def try_crystal(B: Blk, m) -> bool:
    D = B.D
    tips, outs = _host_tips(B, m, "AB")
    why = "the oscillator pins are not on one side of the part"
    if tips is not None and len(outs) == 1 and next(iter(outs))[1] == 0:
        s = next(iter(outs))[0]
        ctx = B._ctx_pins()
        why = "no room at the pins"
        # first with the way out of the waiting supply / ground pins of that side kept free (Blk.rail_lanes): their
        # symbols then stand beyond the neighbours' names instead of sideways on top of them
        for lanes in ((tuple(getattr(B, "rail_lanes", ())), ()) if getattr(B, "rail_lanes", None) else ((),)):
            # (class I) not the first drawing that fits: every drawing that fits is looked at, and the one with the
            # fewest crossings, then the fewest ground symbols, then the shortest wires is drawn
            best = None
            for xo in (0, 4, 8, 12, 16):
                for k in (6, 8, 10, 12):
                    for e in (0, 2, 4, 6, 8, 10, 12):
                        for mir in (None, "x"):
                            for jog in ((False, True) if CRYSTAL_BEST else (False,)):
                                try:
                                    piece = IMOD.instantiate(D, m, ST.crystal_template(D, m, tips, s, k, e * U, mir, xo, jog), ctx)
                                except IdiomError as ex:
                                    why = str(ex)
                                    continue
                                if not B._fits(piece, lanes):
                                    continue
                                if not CRYSTAL_BEST:
                                    B._commit(piece, m)
                                    return True
                                ws = piece["wires"]
                                cross = sum(1 for i in range(len(ws)) for j in range(i + 1, len(ws))
                                            if ws[i][0] != ws[j][0] and IMOD._crosses(ws[i][1], ws[i][2], ws[j][1], ws[j][2]))
                                ln = sum(abs(a_[0] - b_[0]) + abs(a_[1] - b_[1]) for _n, a_, b_ in ws)
                                key = (cross, len(piece["powers"]), round(ln, 2))
                                if best is None or key < best[0]:
                                    best = (key, (k, e, mir, xo, jog))
                if best is not None and best[0][0] == 0:
                    break                                   # (further out only gets longer)
            if best is not None:
                k, e, mir, xo, jog = best[1]
                piece = IMOD.instantiate(D, m, ST.crystal_template(D, m, tips, s, k, e * U, mir, xo, jog), ctx)
                B._commit(piece, m)
                return True
    _drop(B, m, why)
    if tips is not None:
        _give_back(B, m, "AB")
    return False


def try_cc(B: Blk, m) -> bool:
    D = B.D
    tips, outs = _host_tips(B, m, "01")
    why = "the CC pins are not on one side of the connector"
    if tips is not None and len(outs) == 1 and next(iter(outs))[1] == 0:
        s = next(iter(outs))[0]
        ctx = B._ctx_pins()
        why = "no room at the pins"
        plan = m.extra.get("plan") or {}
        a = m.host[0]
        pg = _pin(B, a, plan["g"]) if plan.get("g") is not None else None
        pv = _pin(B, a, plan["v"]) if plan.get("v") is not None else None
        ys = [t[1] for t in tips.values()]
        if pg is not None and not (tuple(pg["out"]) == (s, 0) and pg["xy"][1] < min(ys) - 0.01):
            pg = None                                       # (the part was turned over: the classic drawing)
        if pv is not None and not (pg is not None and tuple(pv["out"]) == (s, 0) and pv["xy"][1] > max(ys) + 0.01):
            pv = None
        extra = [q for q in (_pin(B, a, plan.get(t)) for t in ("g", "v") if plan.get(t) is not None) if q is not None]

        def stubs(drawn):
            for q in extra:
                if not any(q["number"] == d_["number"] for d_ in drawn):
                    _rail_stub(B, q, D.pins_of[a].get(q["number"]))
        variants = []
        if pg is not None:
            if pv is not None:
                m.nets["VB"] = D.pins_of[a].get(pv["number"])
                # (staggered: the lower resistor stands beyond the upper one, its texts above it - the row under
                # it stays free for the supply pin's wire)
                variants += [(stag, k, pg, pv) for stag in (True, False) for k in (0, 2, 4, 6, 8, 12)]
            variants += [(stag, k, pg, None) for stag in (False, True) for k in (0, 2, 4, 6, 8, 12)]
        variants += [(stag, k, None, None) for stag in (False, True) for k in (0, 2, 4, 6, 8, 12)]
        for stag, k, g_, v_ in variants:
            if v_ is None:
                m.nets.pop("VB", None)
            try:
                piece = IMOD.instantiate(D, m, ST.cc_template(
                    D, m, tips, s, k, stag, _tip(g_) if g_ is not None else None, plan.get("mode"),
                    _tip(v_) if v_ is not None else None), ctx)
            except IdiomError as ex:
                why = str(ex)
                continue
            if B._fits(piece):
                B._commit(piece, m)
                stubs([q for q in (g_, v_) if q is not None])
                return True
        m.nets.pop("VB", None)
        _drop(B, m, why)
        _give_back(B, m, "01")
        stubs([])
        return False
    _drop(B, m, why)
    if tips is not None:
        _give_back(B, m, "01")
    plan = m.extra.get("plan") or {}
    for t in ("g", "v"):
        q = _pin(B, m.host[0], plan.get(t)) if plan.get(t) is not None else None
        if q is not None:
            _rail_stub(B, q, D.pins_of[m.host[0]].get(q["number"]))
    return False


def _rail_stub(B: Blk, p, n):
    """A supply pin a stage meant to wire itself and could not: stub and upright symbol, the plain way."""
    D = B.D
    o = tuple(p["out"])
    tip = _tip(p)
    end = (snap(tip[0] + o[0] * STUB), snap(tip[1] + o[1] * STUB))
    gnd = D.is_gnd(n)
    nat = "down" if gnd else "up"
    name = D.text(n)
    opts = []
    natural = o[1] == 0 or (o[1] > 0) == gnd
    for kk in (0, 2, 4, 6, 8, 10, 12) if natural else ():
        e2 = (snap(end[0] + o[0] * kk * U), snap(end[1] + o[1] * kk * U))
        q = e2 if o[1] != 0 else (e2[0], snap(e2[1] + (GAP_ if gnd else -GAP_)))
        ws = ([(n, end, e2)] if kk else []) + ([(n, e2, q)] if q != e2 else [])
        opts.append((q, nat, ws))
    pick = next((x for x in opts if B.free(power_boxes(name, x[0], x[1]) + [wbox(a_, b_, 0.02) for _n, a_, b_ in x[2]])), None)
    if pick is None:
        # nothing upright is free: the symbol lies at the end of the stub (counted and reported as sideways) - a
        # last resort must never lay a wire over what stands there
        pick = (end, ("right" if o[0] > 0 else "left") if o[1] == 0 else "right", [])
    B.wires += [(n, tip, end)] + pick[2]
    B.powers.append((n, pick[0], pick[1], name))
    B.boxes += [wbox(tip, end)] + [wbox(a_, b_) for _n, a_, b_ in pick[2]] + power_boxes(name, pick[0], pick[1])


def sense_wire(B: Blk, m) -> bool:
    """(class I) The feedback / sense pin of a fixed-output converter is on the output rail itself. It is WIRED to
    the output line of the stage - out of the pin, over (or under) the parts of the line, down onto the line between
    two of them - so the loop is on the drawing. The wire touches nothing and crosses nothing; when no such way is
    free the pin gets its supply symbol as before. -> True when the wire is drawn."""
    D = B.D
    a, no, k = m.host[0], m.extra["sense"], m.nets["VOUT"]
    p = _pin(B, a, no)
    if p is None:
        return False
    o = tuple(p["out"])
    tip = _tip(p)
    if o[1] != 0:
        _rail_stub(B, p, k)
        return False
    line = [(w[1], w[2]) for w in B.wires if w[0] == k and abs(w[1][1] - w[2][1]) < 0.01 and abs(w[1][0] - w[2][0]) > 4 * U]
    marks = [w[i] for w in B.wires if w[0] == k for i in (1, 2)] + [q for n2, q, _o, _v in B.powers if n2 == k]
    marks += [_tip(q) for s_ in B.syms for q in s_.pins_world() if D.pins_of.get(s_.ref, {}).get(q["number"]) == k]
    tees = []
    for w0, w1 in line:
        y = w0[1]
        x0, x1 = sorted((w0[0], w1[0]))
        x = snap(x0 + 2 * U)
        while x <= x1 - 2 * U + 0.01:
            if not any(abs(e[1] - y) < 0.01 and abs(e[0] - x) < 2 * U - 0.01 for e in marks):
                tees.append((x, y))
            x = snap(x + 2 * U)
    best = None
    end = (snap(tip[0] + o[0] * STUB), tip[1])
    for tx, ty in tees:
        if (tx - end[0]) * o[0] < 2 * U - 0.01:
            continue
        for s1 in (0, 2, 4):
            x1 = snap(end[0] + o[0] * s1 * U)
            for j in range(0, 12):
                for sg in ((-1, 1) if j else (1,)):
                    yo = snap(tip[1] + sg * j * 2 * U)
                    if abs(yo - ty) < 2 * U - 0.01 and abs(yo - ty) > 0.01:
                        continue
                    pts = [end, (x1, tip[1]), (x1, yo), (tx, yo), (tx, ty)]
                    path = [pts[0]]
                    for q in pts[1:]:
                        if q != path[-1]:
                            path.append(q)
                    segs = list(zip(path, path[1:]))
                    if any(abs(a_[0] - b_[0]) > 0.01 and abs(a_[1] - b_[1]) > 0.01 for a_, b_ in segs):
                        continue
                    bx = []
                    for i, (a_, b_) in enumerate(segs):
                        if i == len(segs) - 1:              # (it ends ON the line: checked up to there)
                            d = (0.6 if b_[0] > a_[0] else -0.6 if b_[0] < a_[0] else 0.0,
                                 0.6 if b_[1] > a_[1] else -0.6 if b_[1] < a_[1] else 0.0)
                            b_ = (b_[0] - d[0], b_[1] - d[1])
                        bx.append(wbox(a_, b_, 0.3))
                    if not B.free(bx):
                        continue
                    ln = sum(abs(a_[0] - b_[0]) + abs(a_[1] - b_[1]) for a_, b_ in segs)
                    key = (len(segs), ln)
                    if best is None or key < best[0]:
                        best = (key, segs)
    if best is None:
        _rail_stub(B, p, k)
        return False
    B.wires.append((k, tip, end))
    B.boxes.append(wbox(tip, end))
    for a_, b_ in best[1]:
        B.wires.append((k, a_, b_))
        B.boxes.append(wbox(a_, b_))
    B.stats["matches"].append("sense")
    return True


def dock_chain(B: Blk, m) -> bool:
    """The power line of a boost (above the controller, SW node over the SW pin's wire end) or a buck (on the SW
    pin's row) converter."""
    D = B.D
    p = _pin(B, m.host[0], m.host[1])
    sense = _pin(B, m.host[0], m.extra["sense"]) if m.extra.get("sense") is not None else None
    if p is None:
        _drop(B, m, "the controller is not placed")
        return False
    c = B._child()
    try:
        piece = IMOD.instantiate(D, m, ST.chain_template(D, m))
    except IdiomError as ex:
        _drop(B, m, str(ex))
        if sense is not None:
            _rail_stub(B, sense, m.nets["VOUT"])
        return False
    c._commit(piece, m)
    o = tuple(p["out"])
    sw = m.nets["SW"]
    tip = (snap(p["xy"][0] + o[0] * STUB), snap(p["xy"][1] + o[1] * STUB))
    port = next(t[1] for t in c.terms if t[0] == sw)
    out = (0, -1) if m.idiom == "boost" else (1, 0)
    ok = False
    if (m.idiom == "boost" and o != (0, 1)) or (m.idiom == "buck" and o == (1, 0)):
        ok = dock_rows(B, c, [(sw, tip, port)], out, kmin=4 if out[1] else None, kmax=72, clr=1.2) > 0
    if not ok:                                              # the line still reads left to right; the router joins SW
        bb, cb = B.bbox(), c.bbox()
        B.attach(c, "T", pref=(tip[0], bb[1]), port=(port[0], cb[3]), gap=4 * U, clr=3.0)
    B.stats["claimed"].add(m.host[0])
    if sense is not None:
        sense_wire(B, m)
    return True


def attach_stages(B: Blk, ref):
    m = B.sm["power"].get(ref)
    if m is not None and not m.extra.get("done"):
        m.extra["done"] = True
        dock_chain(B, m)


# ───────────────────────── peers ─────────────────────────
def _inversions(seq: list) -> int:
    return sum(1 for i in range(len(seq)) for j in range(i + 1, len(seq)) if seq[i] > seq[j])


def dock_anchor(B: Blk, a, prim, _tap=None) -> bool:
    """A second IC / connector of the block, placed FACING the pins of the main IC it shares nets with: posed
    (mirrored if needed) so that as many shared pins as possible look at the host in the host's pin order, shifted
    so that most of them sit on their host pin's row, joined by straight parallel wires."""
    D = B.D
    B.docked = None             # how the peer was joined: ("bundle" | "rows", [nets wired]) - see build_block
    shared = {k for k in D.pins_of[a].values() if D.conn(k) and not D.is_rail(k)}
    host_pins = []
    for s in B.syms:
        if s.ref == a:
            continue
        for p in s.pins_world():
            k = D.pins_of[s.ref].get(p["number"])
            if k in shared and p["out"][1] == 0:
                host_pins.append((p, k))
    if not host_pins or len(D.units[a]) != 1:
        return False
    sides = [SIDE[tuple(p["out"])] for p, _k in host_pins]
    side = max(sorted(set(sides)), key=sides.count)
    out = DIRS[side]
    if _tap is not None:
        out = _tap[3]
        side = SIDE[out]
        _tap = _tap[:3]
    # open wire ends of the host that look the right way, per net (the pin's stub end, or the end of its stem)
    hterm = {}
    for n, pt, o in B.terms:
        if n in shared and tuple(o) == out:
            hterm.setdefault(n, []).append(pt)
    tap = _tap
    if tap:
        B.wires.append(tap)
        B.boxes.append(wbox(tap[1], tap[2]))
        B.terms.append((tap[0], tap[2], out))
        hterm[tap[0]] = [tap[2]]

    def untap():
        if tap:
            B.wires.remove(tap)
            B.terms.remove((tap[0], tap[2], out))
            B.boxes.remove(wbox(tap[1], tap[2]))
            B.used.discard(a)
    if not hterm and _tap is None and TAP and B.v2 and D.is_conn(a) and len(shared) == 1 and D.npins(a) <= 4:
        # (fix round 4) the one signal of a small connector is already taken at the host's pin (the output that
        # drives an LED chain AND goes to a 2-pin terminal): a branch leaves the pin's stub, two to six rows up or
        # down, and hands the connector an open end of its own - the connector is then wired like any other, instead
        # of floating beside the wire with a name
        k = next(iter(shared))
        prim_tips = {(round(q["xy"][0], 2), round(q["xy"][1], 2)) for s_ in B.syms if s_.ref == prim for q in s_.pins_world()}
        for p, k2 in host_pins:
            if k2 != k or p["out"][1] != 0 or (round(p["xy"][0], 2), round(p["xy"][1], 2)) not in prim_tips:
                continue
            o2 = tuple(p["out"])
            tip = (snap(p["xy"][0]), snap(p["xy"][1]))
            e = (snap(tip[0] + o2[0] * 2 * U), tip[1])
            if not any(n2 == k and {a_, b_} == {tip, e} for n2, a_, b_ in B.wires):
                # the pin's wire runs on in one piece (to an in-line part): it is cut at the branch point
                w = next((w for w in B.wires if w[0] == k and tip in (w[1], w[2]) and w[1][1] == w[2][1]
                          and (w[1][0] + w[2][0] - 2 * tip[0]) * o2[0] >= 4 * U - 0.01), None)
                if w is None:
                    continue
                far = w[2] if w[1] == tip else w[1]
                B.wires[B.wires.index(w)] = (k, tip, e)
                B.wires.append((k, e, far))
            for j_ in (2, 3, 4, 5, 6):
                for sg in (1, -1):
                    q = (e[0], snap(e[1] + sg * j_ * 2 * U))
                    if B.free([wbox((e[0], e[1] + sg * 0.8), q, 0.3)]) and dock_anchor(B, a, prim, _tap=(k, e, q, o2)):
                        return True
        return False
    if not hterm:
        return False
    # a doubled pin pair of the host (USB-C D+ on A6 / B6) can hand its wire out at either of its two rows
    halt = {}
    for n, first, tip, ws, o in B.alts:
        if n in hterm and tuple(o) == out and first in hterm[n] and tip not in hterm[n]:
            hterm[n].append(tip)
            halt[(n, tip)] = (first, ws)
    si = D.units[a][0]
    order = sorted(hterm, key=lambda n: min(pt[1] for pt in hterm[n]))
    rank = {n: i for i, n in enumerate(order)}
    best = None
    tried = []
    poses = [(si.rot, si.mirror)] + [(r, m_) for r in (0, 180) for m_ in (None, "y", "x")]
    if D.is_conn(a):                                        # a connector is never turned upside down (pin 1 on top)
        poses = [(si.rot, si.mirror), (si.rot, "y" if si.mirror != "y" else None)]
    for rot, mir in poses:
        set_pose(si, 0.0, 0.0, rot, mir)
        seen, facing, rows = set(), [], {}
        for p in sorted(si.pins_world(), key=lambda p: (p["xy"][1], p["xy"][0])):
            k = D.pins_of[a].get(p["number"])
            if k in rank and tuple(p["out"]) == (-out[0], 0) and k not in seen:
                seen.add(k)
                facing.append(rank[k])
                rows[k] = p["xy"][1]
        # every facing pin on a row the host offers for its net: nothing is out of order, whatever the ranks say
        full = len(rows) >= 2 and any(
            all(any(abs(hp[1] - rows[k] - d) < 0.01 for hp in hterm[k]) for k in rows)
            for d in {round(hp[1] - rows[k0], 2) for k0 in list(rows)[:1] for hp in hterm[k0]})
        allp = [p for p in si.pins_world() if D.conn(D.pins_of[a].get(p["number"]))]
        vert = sum(1 for p in allp if p["out"][0] == 0)
        # ... and its ground pins look down, its supply pins up (the symbols then stand straight on the pins)
        bad = sum(1 for p in allp if p["out"][0] == 0 and D.is_rail(D.pins_of[a].get(p["number"]))
                  and (p["out"][1] > 0) != D.is_gnd(D.pins_of[a].get(p["number"])))
        key = (-len(facing), 0 if full else _inversions(facing), bad, vert)
        tried.append((bad, vert, rot, mir))
        if best is None or key < best[0]:
            best = (key, rot, mir)
    if best is None or best[0][0] == 0:
        untap()
        return False
    special = a in B.sm["usbc"] or any(m.host[0] == a for m in B.sm["crystal"])
    state0 = (set(B.used), list(B.stats["matches"]), list(B.stats["dropped"]), set(B.stats["claimed"]))

    def child(rot, mir):
        """The peer as a cell of its own, posed (its stubs, supply symbols and caps drawn), at the origin. What an
        earlier pose of it claimed (caps, pull-ups) is given back first."""
        B.used.clear()
        B.used.update(state0[0])
        B.stats["matches"][:] = state0[1]
        B.stats["dropped"][:] = state0[2]
        B.stats["claimed"].clear()
        B.stats["claimed"].update(state0[3])
        c_ = B._child()
        c_.no_stem = getattr(B, "no_stem", ())               # (reservation) parts on a bundle net wait for its wire
        B.used.add(a)
        place_unit(c_, a, 0, (0.0, 0.0)) if special else c_.place(si, (0.0, 0.0), rot=rot, mirror=mir)
        B.add_banks(c_, a)
        return c_
    c = child(best[1], best[2])
    cterm = {}
    for n, pt, o in c.terms:
        if n in hterm and tuple(o) == (-out[0], 0):
            cterm.setdefault(n, []).append(pt)
    votes: dict = {}
    for n, pts in cterm.items():
        for hp in hterm[n]:
            for cp in pts:
                d = round(hp[1] - cp[1], 2)
                votes.setdefault(d, set()).add(n)
    if tap and not votes:
        untap()
        return False
    if not votes:
        B.attach(c, side, None, gap=8 * U, clr=4.0)
        return True
    hy = [pt[1] for pts in hterm.values() for pt in pts]
    dy = min(votes, key=lambda d: (-len(votes[d]), abs(d - (sum(hy) / len(hy))), d))
    edge = max(pt[0] * out[0] for pts in hterm.values() for pt in pts) * out[0]
    pairs = []
    for n in sorted(votes[dy], key=lambda n: rank[n]):
        hp = next(hp for hp in hterm[n] for cp in cterm[n] if round(hp[1] - cp[1], 2) == dy)
        cp = next(cp for cp in cterm[n] if round(hp[1] - cp[1], 2) == dy)
        pairs.append((n, hp, cp))
    if BUNDLE.ENABLED and B.v2 and not tap:
        # (class L) the shared nets cannot all run straight, row on row (the pin orders differ, or the peer has bus
        # pins on both sides): they are planned as ONE bundle of parallel wires - every net of the pair wired, or
        # none. The pose is searched too: the one with the fewest crossings wins
        want = {n for n in hterm if any(t[0] == n and t[2][1] == 0 for t in c.terms)}
        if len(want) >= 2 and len(pairs) < len(want):
            skip = {tip for (_n, tip) in halt}
            pose = built = (best[1], best[2])
            pl = BUNDLE.find(B, c, hterm, out, skip)
            if not special and (pl is None or pl.cross):
                bad0 = min(t_[0] for t_ in tried)
                for rot, mir in dict.fromkeys((t_[2], t_[3]) for t_ in sorted(tried, key=lambda t_: t_[:2])
                                              if t_[0] <= bad0):
                    if (rot, mir) == (best[1], best[2]):
                        continue
                    c = child(rot, mir)
                    built = (rot, mir)
                    p2 = BUNDLE.find(B, c, hterm, out, skip)
                    if p2 is not None and (pl is None or p2.key() < pl.key()):
                        pl, pose = p2, (rot, mir)
                if built != pose:
                    c = child(*pose)
                    pl = BUNDLE.find(B, c, hterm, out, skip) if pl is not None else None
            if pl is not None:
                BUNDLE.commit(B, c, pl, out)
                B.stats["matches"].append("bundle")
                B.stats["claimed"].add(a)
                B.docked = ("bundle", list(pl.nets))
                B.wired = dict(getattr(B, "wired", {}), **{a: list(pl.nets)})
                return True
    if BUS_WHOLE and B.v2:
        # (fix round 3) a bus between the two parts (three or more names with one prefix) is drawn ONE way: when
        # only some of its members can be wired row on row (the flash pins that look at the MCU), none is
        hosts = {s.ref for s in B.syms if s.ref != a}
        bus: dict = {}
        for k in shared:
            pre = ROUTE.bus_prefix(D.text(k)) if D.named(k) else ""
            if pre and any(r in hosts and D.npins(r) >= 5 for r, _p in D.view.nets[k].pins):
                bus.setdefault(pre, set()).add(k)
        wired = {n for n, _hp, _cp in pairs}
        for ks in bus.values():
            if len(ks) >= 3 and not ks <= wired:
                pairs = [x for x in pairs if x[0] not in ks]
        if not pairs:
            B.attach(c, side, None, gap=8 * U, clr=4.0)
            return True
    # the first pair fixes the offset; the cell starts beyond the outermost host wire end of the shared nets
    n0, hp0, cp0 = pairs[0]
    kmin = max(max(_label_steps(D, n) for n in votes[dy]), int(round(abs(edge - hp0[0]) / U)) + 2 * len(pairs) + 2)
    kmin += kmin % 2
    for n, hp, _cp in pairs:
        if (n, hp) in halt:
            # the pair hands its wire out at its other row: the open end (and the room for its name) moves there
            first, ws = halt[(n, hp)]
            B.wires += ws
            B.boxes += [wbox(a_, b_) for _n, a_, b_ in ws]
            B.terms = [t if not (t[0] == n and t[1] == first) else (n, hp, t[2]) for t in B.terms]
            B.resv = [(n2, b) for n2, b in B.resv
                      if not (n2 == n and b[0] - 0.4 <= first[0] <= b[2] + 0.4 and b[1] - 0.4 <= first[1] <= b[3] + 0.4)]
            if D.named(n):
                B.resv.append((n, lbl_box(D.text(n), hp, out, D.label_kind(n))))
            B.alts = [x for x in B.alts if not (x[0] == n and x[1] == first)]
    got = dock_rows(B, c, pairs, out, kmin=kmin, kmax=kmin + 60, clr=2.0, need=max(1, (len(pairs) + 1) // 2))
    if got:
        if got >= 2:
            B.stats["matches"].append("peer")
            B.stats["claimed"].add(a)
        B.docked = ("rows", [n for n, _hp, _cp in pairs])
        if got >= len(pairs):
            B.wired = dict(getattr(B, "wired", {}), **{a: [n for n, _hp, _cp in pairs]})
        return True
    if tap:
        untap()
        return False
    pref = (hp0[0], hp0[1])
    r = round((hp0[1] - cp0[1]) % (2 * U), 2)
    if 0.01 < r < 2 * U - 0.01:
        # the slide search moves in 2.54 steps: start it on the host's row grid, or every wire gets a jog
        c.shift(0.0, r)
        cp0 = (cp0[0], snap(cp0[1] + r))
    B.attach(c, side, pref, port=cp0, gap=8 * U, clr=4.0)
    return True


# ───────────────────────── the block ─────────────────────────
def _chain_unit(B: Blk, c) -> bool:
    """A further stage (amplifier unit) whose input comes from an output this block already has: to its right, the
    two on one row."""
    for n, pt, o in list(c.terms):
        if tuple(o) != (-1, 0):
            continue
        for hn, hp, ho in sorted(B.terms, key=lambda t: (-t[1][0], t[1][1])):
            if hn == n and tuple(ho) == (1, 0):
                if dock_rows(B, c, [(n, hp, pt)], (1, 0), clr=1.5) > 0:
                    return True
    return False


def dock_nodes(B: Blk, left: list):
    """(package B) No floating islands. The pull-ups, pull-downs, filter caps and dividers that found no room right
    at the pin they serve (a neighbour's row was in the way) used to become a fragment of their own, joined to the
    pin by nothing but a net name. They are drawn ON the net's wire instead, where it ends in this block:

        pin ── NAME ─────────┬──      the wire runs on until the parts are free          pin ── NAME ─┐
                            [R]       (pull-up straight above, pull-down straight                     └──┬──
                             ⏚        below); when the row itself is blocked, the wire               [R]
                                      leaves it with ONE bend and the parts hang on the branch         ⏚
    The drawing is a node template proven by `instantiate`; whatever still finds no room stays for the caller."""
    from .idioms import match as IM
    from .idioms import passive as IP
    from .idioms.model import instantiate
    D = B.D
    nets = []
    for r in left:
        if r in B.used or not IM.node_part(D, r):
            continue
        for k in D.pins_of[r].values():
            if D.conn(k) and not D.is_rail(k) and k not in nets:
                nets.append(k)
    for k in nets:
        m = IM.node_match(D, B.info, k, B.used, None)
        if m is None or not m.roles or m.idiom == "led" or "series" in m.roles                 or any(r not in B.sat_refs or r in B.used for r in m.refs()):
            continue
        ctx = B._ctx_pins()
        room = IP._label_room(D, k) if D.named(k) else 2
        done = False
        for t in [t for t in B.terms if t[0] == k and t[2][1] == 0]:
            tip, d = t[1], t[2][0]
            old_resv = lbl_box(D.text(k), tip, t[2], D.label_kind(k)) if D.named(k) else None
            keep_terms, keep_resv = list(B.terms), list(B.resv)
            B.terms = [x for x in B.terms if x is not t]
            if old_resv is not None:
                B.resv = [x for x in B.resv if not (x[0] == k and x[1] == old_resv)]
            # the wire that ends at the tip is where the drawing starts: it is no obstacle to its own extension
            own = [wbox(a, b) for n, a, b in B.wires if n == k and tip in (tuple(a), tuple(b))]
            keep_boxes = list(B.boxes)
            B.boxes = [x for x in B.boxes if x not in own]
            cands = [(0, 0, j) for j in range(room, 44, 2)]
            # a branch: the shortest one first. The wire of a top / bottom pin simply runs on (no step sideways first)
            vert = [1 for n, a, b in B.wires if n == k and tip in (tuple(a), tuple(b)) and abs(a[0] - b[0]) < 0.01]
            steps = (0,) if vert else (room, room + 4, 0)
            if vert:
                cands = []                 # ... and its parts hang beside that run, not on a row over the part
            cands += sorted(((jr, sg * rows * 2 * U, j) for rows in RISER_ROWS for sg in (-1, 1) for jr in steps
                             for j in (2, 4, 6, 10, 16, 22)), key=lambda c: (c[0] + c[2] + abs(c[1]) / U, abs(c[1]), c[1]))
            for jr, dy, j in cands:
                try:
                    if dy:
                        corner = (snap(tip[0] + d * jr * U), tip[1])
                        base = (corner[0], snap(tip[1] + dy))
                        T = IP.node_template(D, m, base, d, j)
                        if T is not None:
                            T.wires.append(("N", [tip, corner, base]))
                    else:
                        T = IP.node_template(D, m, tip, d, j)
                    if T is None:
                        continue
                    piece = instantiate(D, m, T, ctx)
                except IdiomError:
                    continue
                if B._fits(piece):
                    B.boxes = keep_boxes
                    B._commit(piece, m)
                    done = True
                    break
            if done:
                break
            B.terms, B.resv, B.boxes = keep_terms, keep_resv, keep_boxes


def dock_points(B: Blk, left: list):
    """(package B) A one-pin part (test point, pad) stands ON the wire it probes: its pin tip on the wire of the pin
    it serves, the wire running on just far enough for the net's name and for the part to stand free - above the
    wire when there is room, else below. No hook under the part, no fragment of its own.

        pin ── NAME ──┬        (the tip of the test point IS the wire end)
                      ○ TP1"""
    from .build import pose_pin
    from .idioms import passive as IP
    D = B.D
    for r in left:
        po = D.pins_of.get(r, {})
        if r in B.used or len(po) != 1 or len(D.units.get(r, ())) != 1:
            continue
        (pn, k), = po.items()
        if not D.conn(k) or D.is_rail(k):
            continue
        si = D.units[r][0]
        room = IP._label_room(D, k) if D.named(k) else 2
        done = False
        for t in [t for t in B.terms if t[0] == k and t[2][1] == 0]:
            tip, d = t[1], t[2][0]
            old_resv = lbl_box(D.text(k), tip, t[2], D.label_kind(k)) if D.named(k) else None
            own = [wbox(a, b) for n, a, b in B.wires if n == k and tip in (tuple(a), tuple(b))]
            occ = [x for x in B.boxes if x not in own] + [x[1] for x in B.resv if not (x[0] == k and x[1] == old_resv)]
            ctx = B._ctx_pins()
            for out in ((0, 1), (0, -1)):                    # standing on the wire first, hanging under it second
                for j in range(room, 40, 2):
                    tgt = (snap(tip[0] + d * j * U), tip[1])
                    run = wbox(tip, tgt, 0.02)
                    if any(hit(run, o) for o in occ):
                        break                                # the row itself is blocked from here on
                    if any(kk != k and abs(q[1] - tip[1]) < 0.01
                           and min(tip[0], tgt[0]) - 0.01 <= q[0] <= max(tip[0], tgt[0]) + 0.01 for q, kk in ctx.items()):
                        break
                    if not pose_pin(si, pn, tgt, out):
                        break
                    bx = sym_boxes(si)
                    # (a part that is not fitted is drawn with a cross that is larger than its body)
                    wide = [[bx[0][0] - 1.3, bx[0][1] - 1.3, bx[0][2] + 1.3, bx[0][3] + 1.3]] + bx[1:]
                    if any(hit(b_, o) for b_ in wide for o in occ):
                        continue
                    B.terms = [x for x in B.terms if x is not t] + [(k, tgt, t[2])]
                    if old_resv is not None:
                        B.resv = [x for x in B.resv if not (x[0] == k and x[1] == old_resv)]
                    B.wires.append((k, tip, tgt))
                    B.boxes += bx + [wbox(tip, tgt)]
                    B.syms.append(si)
                    B.used.add(r)
                    done = True
                    break
                if done:
                    break
            if done:
                break


RESERVE = True             # class L: a block whose peers are left with names is drawn again with the parts on the shared
#                            nets deferred / the shared rows kept free, and the best drawing is kept (build_block)
RESERVE_PHANTOM = False    # ... and a third time with the peers' whole place kept free (docked on the bare IC first).
#                            Off: on the bench it never beat the other two, and it costs two more drawings of the block
RESERVE_AIR = 1.0
_CHOICE: dict = {}         # what build_block chose for a block before (the engine draws a sheet several times)


TAP_ROWS = (2, 3, 4, 5, 6, 7, 8, 9, 10)


def _branch(B: Blk, k, x, y0, y1):
    """A vertical branch of net k at x from y0 (on its wire) to y1: 0 when it is free, 1 when it crosses ONE wire
    of another net squarely (2.54 mm or more from that wire's ends and from both ends of the branch), else None."""
    sg = 1 if y1 > y0 else -1
    box = [x - 0.3, min(y0 + sg * 0.8, y1), x + 0.3, max(y0 + sg * 0.8, y1)]
    hits = [o for o in B.occ() if hit(box, o, 0)]
    if not hits:
        return 0
    if len(hits) > 1:
        return None
    key = tuple(round(v, 2) for v in hits[0])
    for n, p, q in B.wires:
        if tuple(round(v, 2) for v in wbox(p, q)) != key:
            continue
        if n == k or abs(p[1] - q[1]) > 0.01:
            return None
        if not (min(p[0], q[0]) + 2 * U - 0.01 <= x <= max(p[0], q[0]) - 2 * U + 0.01):
            return None
        if not (min(y0, y1) + 2 * U - 0.01 <= p[1] <= max(y0, y1) - 2 * U + 0.01):
            return None
        if any(max(abs(e[0] - x), abs(e[1] - p[1])) < 2.53 for _n, a_, b_ in B.wires for e in (a_, b_)):
            return None
        return 1
    return None


_CELL_LISTS = ("boxes", "resv", "wires", "powers", "ncs", "terms", "labels", "flags", "syms", "fallback", "alts",
               "bars", "name_at", "runs")
TAP_TRIES = 8
TAP_PULL = True            # a lone pull resistor on a bundle wire: upright over it or in line beyond its turn (tap_deferred)
TAP_TRACK = True           # a deferred part may hang on the end of the bundle wire's track (tap_deferred)
TAP_SPOTS = 16             # tee positions tried along the first piece of the bundle wire (was 6: only right at the pins)
PEER_IN_LINE = True        # class L: a peer on one net of a top / bottom pin is attached with its open end in line with the pin
TAP_W, TAP_H = 1.0, 0.5     # what a millimetre of block width / height costs when a deferred part is tapped


def _cell_state(B: Blk) -> dict:
    st = {k: list(getattr(B, k)) for k in _CELL_LISTS}
    st["used"] = set(B.used)
    st["stats"] = (list(B.stats["matches"]), list(B.stats["dropped"]), set(B.stats["claimed"]))
    return st


def _cell_restore(B: Blk, st: dict):
    for k in _CELL_LISTS:
        setattr(B, k, list(st[k]))
    B.used.clear()
    B.used.update(st["used"])
    B.stats["matches"][:] = st["stats"][0]
    B.stats["dropped"][:] = st["stats"][1]
    B.stats["claimed"].clear()
    B.stats["claimed"].update(st["stats"][2])


def tap_deferred(B: Blk):
    """The parts that waited for a bundle (`Blk.no_stem`: the boot switch and its resistor on a flash's chip select)
    and that do not simply hang on its wire: a branch leaves the bundle wire behind the net's name - or where the
    wire turns, the row running on a little - a few rows up or down, and the part is docked on the branch's row like
    on any open wire end, towards the IC or away from it. The branch may cross one wire squarely. Of the first
    `TAP_TRIES` drawings that work, the one that makes the block grow least is kept (a crossing counts as 10 mm).

        pin ── NAME ──┬───────── ... peer
          [parts] ────┘"""
    D = B.D
    for k, h, e, out, turns in list(B.runs):
        if k not in getattr(B, "no_stem", ()):
            continue
        refs = [x[4] for x in B.elems_on(k) if x[4] not in B.used and x[4] in B.sat_refs]
        if not refs or out[1] != 0:
            continue
        s = out[0]
        r = sorted(refs, key=_refkey)[0]
        xs = []                                             # (x, piece the row runs on by, row of the tee, way)
        x = h[0] + s * BUNDLE.name_room(D, k)
        while (e[0] - x) * s >= 2 * 2 * U - 0.01:
            xs.append((snap(x), None, h[1], 0))
            x += s * 2 * U
        xs = xs[:TAP_SPOTS]
        if turns:                                           # ... or beyond the turn: the row runs on, a tee at the turn
            for i in range(1, 7):
                x = snap(e[0] + s * i * 2 * U)
                if not B.free([wbox((e[0] + s * 0.8, e[1]), (x, e[1]), 0.3)]):
                    break
                xs.append((x, (k, e, (x, e[1])), h[1], 0))
            # ... or beyond the SECOND turn: the track runs on past the row of the peer's pin (a part that stands
            # above / below its host rows - the comb of bundle._find - leaves no room at the rows themselves)
            v = next(((a_, b_) for n_, a_, b_ in B.wires if n_ == k and abs(a_[0] - b_[0]) < 0.01
                      and tuple(e) in (tuple(a_), tuple(b_))), None)
            if TAP_TRACK and v is not None:
                top = v[1] if tuple(v[0]) == tuple(e) else v[0]
                xs.append((top[0], None, top[1], 1 if top[1] > e[1] else -1))
        bb0 = B.bbox()
        st0 = _cell_state(B)

        def attempt(x, ext, y0, j, sg, d):
            tee, end = (x, y0), (x, snap(y0 + sg * j * 2 * U))
            ws = ([ext] if ext else []) + [(k, tee, end)]
            B.wires += ws
            B.boxes += [wbox(a_, b_) for _n, a_, b_ in ws]
            B.terms.append((k, end, d))
            c = B.sub_cell(r, port=(k, d))
            ports = [q[1] for q in c.terms if q[0] == k and tuple(q[2]) == (-d[0], -d[1])]
            return bool(ports) and dock_rows(B, c, [(k, end, ports[0])], d, clr=1.5) > 0
        # (fix round 8) a lone pull-up / pull-down that waited for the bundle: upright right over (under) the wire
        # when that is free; else, when the wire turns, in line with the row running on beyond the turn, its supply
        # symbol upright at the far end. Hung on a branch below the wire a pull-up is entered from underneath:
        # a hairpin (right, down, back left, up)
        po = D.pins_of.get(r, {})
        rails_ = [n_ for n_ in po.values() if D.is_rail(n_)]
        if TAP_PULL and len(refs) == 1 and len(po) == 2 and len(rails_) == 1 and len(D.units.get(r, ())) == 1:
            vd = (0, 1) if D.is_gnd(rails_[0]) else (0, -1)
            done = False
            for x, ext, y0, way in xs:
                if way or ext is not None:
                    continue
                B.terms.append((k, (x, y0), vd))
                c = B.sub_cell(r, port=(k, vd))
                ports = [q[1] for q in c.terms if q[0] == k and tuple(q[2]) == (-vd[0], -vd[1])]
                if ports and dock_rows(B, c, [(k, (x, y0), ports[0])], vd, kmax=12, clr=1.5) > 0:
                    done = True
                    break
                _cell_restore(B, st0)
            for x, ext, y0, way in ([] if done else xs):
                if way or ext is None:
                    continue
                end = (x, y0)
                B.wires.append(ext)
                B.boxes.append(wbox(ext[1], ext[2]))
                B.terms.append((k, end, (s, 0)))
                c = B.sub_cell(r, port=(k, (s, 0)), flat=True)
                ports = [q[1] for q in c.terms if q[0] == k and tuple(q[2]) == (-s, 0)]
                if ports and dock_rows(B, c, [(k, end, ports[0])], (s, 0), kmax=16, clr=1.5) > 0:
                    done = True
                    break
                _cell_restore(B, st0)
            BUNDLE._dbg("tap pull", r, D.text(k), "done" if done else "no place in line")
            if done:
                B.fallback.append(r)
                continue
        found = []
        for cross in (0, 1):
            for j in TAP_ROWS:
                for sg in (-1, 1):
                    for x, ext, y0, way in xs:
                        if way and sg != way:
                            continue
                        if len(found) >= TAP_TRIES or _branch(B, k, x, y0, snap(y0 + sg * j * 2 * U)) != cross:
                            continue
                        for d in ((-s, 0), (s, 0)):
                            ok = attempt(x, ext, y0, j, sg, d)
                            # ... but never back past the pins it comes from: a pull-up drawn on the far side of
                            # its IC at the end of a wire around the part is no drawing of a pull-up
                            if ok and d[0] == -s and any(
                                    (si_.at[0] - h[0]) * s < 0 for si_ in B.syms if si_.ref == r):
                                ok = False
                            if ok:
                                bb = B.bbox()
                                grow = TAP_W * ((bb[2] - bb[0]) - (bb0[2] - bb0[0])) + TAP_H * ((bb[3] - bb[1]) - (bb0[3] - bb0[1]))
                                found.append((grow + 10.0 * cross + 0.1 * j, len(found), x, ext, y0, j, sg, d))
                            _cell_restore(B, st0)
        BUNDLE._dbg("tap", r, D.text(k), "run", h, e, "turns", turns, "spots", len(xs), "found", [(round(f[0], 1), f[2], f[4:]) for f in found])
        if found:
            _c, _i, x, ext, y0, j, sg, d = min(found)
            if attempt(x, ext, y0, j, sg, d):
                B.fallback.append(r)
            else:
                _cell_restore(B, st0)


def block_links(B: Blk) -> int:
    """Wires missing inside the block as it stands: per signal net, the groups its pins fall into (joined by the
    block's wires only) minus one. The number `score.label_only_links` reports for the finished sheet, before the
    router has had its turn."""
    D = B.D
    tips: dict = {}
    for s in B.syms:
        for p in s.pins_world():
            k = D.pins_of.get(s.ref, {}).get(p["number"])
            if D.conn(k) and not D.is_rail(k):
                tips.setdefault(k, set()).add((snap(p["xy"][0]), snap(p["xy"][1])))
    n = 0
    for k, pts in tips.items():
        if len(pts) < 2:
            continue
        fake = [(k, q, None) for q in sorted(pts)]
        comps = ROUTE.components(B.wires, k, fake)
        on = sum(len(ts) for _cs, ts in comps)
        n += sum(1 for _cs, ts in comps if ts) + (len(pts) - on) - 1
    return n


def peer_links(B: Blk, nets) -> int:
    """Wires missing between DIFFERENT parts on the nets the main IC shares with its peers (a net whose second
    group is just another pin of a part that is already on the first - the far side of a pass-through TVS array -
    does not count: no drawing of the block changes that)."""
    D = B.D
    at = {}
    for s_ in B.syms:
        for p in s_.pins_world():
            k = D.pins_of.get(s_.ref, {}).get(p["number"])
            if k in nets:
                at.setdefault(k, []).append((s_.ref, (snap(p["xy"][0]), snap(p["xy"][1]))))
    n = 0
    for k, pins_ in at.items():
        fake = [(k, q, None) for q in sorted({q for _r, q in pins_})]
        comps = ROUTE.components(B.wires, k, fake)
        grp = {}
        for i, (_cs, ts) in enumerate(comps):
            for t in ts:
                grp[t[1]] = i
        groups: dict = {}
        for r, q in pins_:
            groups.setdefault(grp.get(q, ("solo", q)), set()).add(r)
        gl = list(groups.values())
        real = [g for i, g in enumerate(gl) if not any(j != i and g <= h and (g < h or j < i) for j, h in enumerate(gl))]
        n += max(0, len(real) - 1)
    return n


def islands(B: Blk) -> list:
    """[(ref, net)]: the two-pin parts of the block that hang between a supply / ground and ONE signal net (pull-up,
    pull-down, filter cap) and are joined to no other part on that net by a wire - although the block holds a pin
    of that net. Drawn like that they are an island: a part beside its IC with nothing but the net's name."""
    from .idioms import match as IM
    D = B.D
    tips: dict = {}
    for s in B.syms:
        for p in s.pins_world():
            k = D.pins_of.get(s.ref, {}).get(p["number"])
            if D.conn(k) and not D.is_rail(k):
                tips.setdefault(k, []).append((s.ref, (snap(p["xy"][0]), snap(p["xy"][1]))))
    out = []
    for r in B.sat_refs:
        po = D.pins_of.get(r, {})
        if len(D.units.get(r, ())) != 1 or not IM.two_pin(D, r):
            continue
        sig = [k for k in po.values() if D.conn(k) and not D.is_rail(k)]
        if len(sig) != 1 or not any(D.is_rail(k) for k in po.values()):
            continue
        k = sig[0]
        pins_ = tips.get(k, [])
        mine = [q for r2, q in pins_ if r2 == r]
        if not mine or not any(r2 != r for r2, _q in pins_):
            continue
        comps = ROUTE.components(B.wires, k, [(k, q, None) for q in sorted({q for _r, q in pins_})])
        grp = next((ts for _cs, ts in comps if any(t[1] == mine[0] for t in ts)), [])
        on = {t[1] for t in grp}
        if not any(r2 != r and q in on for r2, q in pins_):
            out.append((r, k))
    return out


def _switches() -> int:
    """Fingerprint of the module switches a drawing depends on (the remembered choice of `build_block` is only
    good for the rules it was made under)."""
    import sys
    from . import build as BUILD
    from .idioms import match as IM_
    from .idioms import passive as IP_
    mods = [sys.modules[__name__], BUILD, BUNDLE, ROUTE, ST, IM_, IP_, sys.modules.get(__package__ + ".engine")]
    return hash(tuple((m.__name__, k, v) for m in mods if m is not None for k, v in sorted(vars(m).items())
                      if k.isupper() and isinstance(v, (bool, int, float, str))))


def _fresh(B: Blk) -> Blk:
    b = Blk(B.D, B.info)
    for k in ("flag_on_wire", "face_hint"):
        if hasattr(B, k):
            setattr(b, k, getattr(B, k))
    b.enable_v2()
    return b


STAGE_IDIOMS = ("crystal", "usbc", "opamp", "ldo", "boost", "buck", "led")


def block_cost(B: Blk) -> int:
    """What a drawing of the block is judged by when two are compared: wires missing inside it, and three for every
    stage idiom (crystal, LED chain ...) that lost its canonical drawing."""
    return block_links(B) + 3 * sum(1 for i, _w in B.stats["dropped"] if i in STAGE_IDIOMS)


def build_block(B: Blk, origin=(0.0, 0.0)):
    """The block, drawn by `_build`. A block with a second IC / connector that is left with names where wires
    belong is drawn again, two more ways, and the best drawing is kept (`block_cost`; a draw: the earlier one):

      1. plain (as before)
      2. the parts that hang on a net the main IC shares with a peer (a pull-up on RESET, the boot switch on a
         flash's chip select) wait until the peer is wired: first-fit placement gave them the room right in front of
         the pins, and the peer that came later found its rows blocked. They are then drawn on the bundle wire
      2b. as 2, and the ROWS of those nets stay free while the main IC's other parts are placed (`Blk.row_lanes`)
      3. as 2, with RESERVATIONS: the peers are docked on the bare IC first (a scratch block); what they and their
         wires occupy there is kept free while the real block is built"""
    D, info = B.D, B.info
    anchors = list(info.anchors)
    prim = info.id if info.id in anchors else None
    if not (RESERVE and BUNDLE.ENABLED and B.v2 and prim and len(anchors) > 1 and len(D.units[prim]) == 1):
        return _build(B, origin)
    mine = {k for k in D.pins_of[prim].values() if D.conn(k) and not D.is_rail(k)}
    shared = {k for a in anchors if a != prim for k in D.pins_of[a].values() if k in mine}
    ckey = (prim, tuple(anchors), tuple(sorted(info.sats)), getattr(B, "face_hint", None),
            getattr(B, "flag_on_wire", None), _switches(),
            hash(tuple((r, tuple(sorted((str(pn), str(k)) for pn, k in D.pins_of[r].items())))
                       for r in sorted(list(anchors) + list(info.sats)))))
    known = _CHOICE.get(ckey)
    if known is not None and known[0] == "reserved":
        known = None                                        # (that drawing needs its scratch block again)
    si = D.units[prim][0]
    if known is not None and known[0] != "plain":
        B.no_stem = set(known[1])
        B.row_lanes = set(known[1]) if known[0] == "lanes" else set()
        _build(B, origin)
        B.stats["matches"].append(known[0])
        return
    _build(B, origin)
    if known is not None:
        return
    best, cost, last = "plain", block_cost(B), "plain"
    if len(_CHOICE) > 400:
        _CHOICE.clear()
    _CHOICE[ckey] = ("plain",)
    if not shared or peer_links(B, shared) == 0:
        return
    # only the nets of the peers that ARE left with names wait: a peer that is wired as it stands (the reference
    # at the converter's VREF pin) keeps its place - deferring its net too traded one name column for another
    need = set()
    for a in anchors:
        na = {k for k in D.pins_of[a].values() if k in mine} if a != prim else set()
        if na and peer_links(B, na) > 0:
            need |= na
    shared = need or shared
    pose = (si.rot, si.mirror)
    made = {"plain": B}

    def attempt(name, nets, phantom, lanes=False):
        nonlocal best, cost, last
        R = _fresh(B)
        R.no_stem = set(nets)
        R.row_lanes = set(nets) if lanes else set()
        try:
            _build(R, origin, phantom, pose)
        except Exception as ex:  # noqa: BLE001 - an extra drawing that fails is simply not taken
            BUNDLE._dbg(name, "failed", type(ex).__name__, ex)
            return
        last = name
        made[name] = R
        c = block_cost(R)
        BUNDLE._dbg("block", info.id, name, "cost", c, "against", best, cost)
        if c < cost:
            best, cost = name, c
    if shared:
        attempt("deferred", shared, None)
    if cost > 0 and shared:
        attempt("lanes", shared, None, True)
    if cost > 0 and shared and RESERVE_PHANTOM:
        S = _fresh(B)                                       # scratch: the main IC with plain stubs, then its peers
        S.used.add(prim)
        S.no_stem = set(mine)
        phantom, nets = {}, set()
        try:
            S.place(si, origin, rot=pose[0], mirror=pose[1])
            for a in anchors:
                if a == prim:
                    continue
                n0 = len(S.boxes)
                if dock_anchor(S, a, prim) and S.docked:
                    e = RESERVE_AIR
                    phantom[a] = [[b[0] - e, b[1] - e, b[2] + e, b[3] + e] for b in S.boxes[n0:]]
                    nets |= set(S.docked[1])
        except Exception as ex:  # noqa: BLE001 - the scratch drawing is advice only
            BUNDLE._dbg("scratch failed", type(ex).__name__, ex)
            phantom = {}
        last = "scratch"
        if phantom:
            attempt("reserved", nets, phantom)
    if last != best:                                        # (the symbols were posed again by the later drawings)
        keep = _fresh(B)
        if best == "plain":
            _build(keep, origin)
        else:
            keep.no_stem = set(made[best].no_stem)
            keep.row_lanes = set(made[best].row_lanes)
            _build(keep, origin, getattr(made[best], "phantom_used", None), pose)
    else:
        keep = made[best]
    _CHOICE[ckey] = (best, frozenset(shared))
    if best != "plain":
        keep.stats["matches"].append(best)
    B.__dict__.clear()
    B.__dict__.update(keep.__dict__)


def _build(B: Blk, origin=(0.0, 0.0), phantom=None, pose=None):
    D, info = B.D, B.info
    anchors = list(info.anchors)
    prim = info.id if info.id in anchors else None
    face = {"input": "right", "io": "left" if getattr(info, "stage", 90) >= 3 else "right"}.get(info.kind)
    if prim and D.is_conn(prim) and getattr(B, "face_hint", None):
        face = B.face_hint
    held: dict = {}

    def release(a=None):
        gone = [b for k in ([a] if a is not None else list(held)) for b in held.pop(k, [])]
        if gone:
            B.boxes = [b for b in B.boxes if not any(b is g for g in gone)]
    if prim:
        B.used.add(prim)
        B.phantom_used = phantom
        if phantom:
            held = {a: list(bs) for a, bs in phantom.items()}
            B.boxes += [b for bs in held.values() for b in bs]
        place_unit(B, prim, 0, origin, face)
        if phantom and (D.units[prim][0].rot, D.units[prim][0].mirror) != pose:
            release()                                       # the IC stands differently: the reservations mean nothing
        if B._power_unit(prim) is None:
            B.add_banks(B, prim)
        attach_stages(B, prim)
    for a in anchors:
        if a == prim:
            continue
        release(a)
        if not (prim and dock_anchor(B, a, prim)):
            shared = set(D.pins_of[a].values()) - D.rails
            pts = [(p["xy"], tuple(p["out"])) for p in (D.units[prim][0].pins_world() if prim else [])
                   if D.pins_of[prim].get(p["number"]) in shared and D.conn(D.pins_of[prim].get(p["number"]))]
            if pts:
                sides = [SIDE[o] for _xy, o in pts]
                side = max(sorted(set(sides)), key=sides.count)
                sel = [xy for (xy, o), s in zip(pts, sides) if s == side]
                pref = (sum(p[0] for p in sel) / len(sel), sum(p[1] for p in sel) / len(sel))
            else:
                side, pref = "R", None
            c = B._child()
            B.used.add(a)
            place_unit(c, a, 0, (0.0, 0.0), {"R": "left", "L": "right"}.get(side) if D.is_conn(a) else None)
            B.add_banks(c, a)
            # (class L) a peer on ONE net of a top / bottom pin (the reference at a converter's VREF pin) stands with
            # its open end of that net in line with the pin, joined by one straight wire - wherever the block's
            # other parts have moved to. (It used to be dropped by the slide search and left to the router, which
            # wired it or not, as the place happened to allow.)
            port, docked = None, False
            if PEER_IN_LINE and B.v2 and side in ("T", "B") and len(sel) == 1:
                ks = {t[0] for t in c.terms if t[0] in shared and D.conn(t[0]) and not D.is_rail(t[0])}
                ends = [t[1] for t in c.terms if t[0] in ks]
                if len(ks) == 1 and ends:
                    k1 = next(iter(ks))
                    port = min(ends, key=lambda q: (-q[1] if side == "T" else q[1], q[0]))
                    o1 = DIRS[side]                             # (the open end of the pin's own stub, whichever way
                    tips = [t[1] for t in B.terms if t[0] == k1    # its name looks: straight over / under the pin)
                            and abs(t[1][0] - sel[0][0]) < 0.01 and (t[1][1] - sel[0][1]) * o1[1] > 0.01]
                    t1 = next((t for t in c.terms if t[0] == k1 and t[1] == port), None)
                    for j in range(0, 6) if tips and t1 is not None else ():
                        # ... the peer's own wire runs on a little when its parts' texts stand in the way of the line
                        ext = None
                        if j and t1[2][1] == 0:
                            far = (snap(port[0] + t1[2][0] * j * 2 * U), port[1])
                            ext = (k1, port, far)
                            run = wbox((port[0] + t1[2][0] * 0.6, port[1]), far, 0.3)
                            if any(hit(run, o_) for o_ in c.boxes + [b_ for n_, b_ in c.resv if n_ != k1]):
                                break
                            c.wires.append(ext)
                            c.boxes.append(wbox(port, far))
                            c.terms = [(k1, far, t[2]) if t is t1 else t for t in c.terms]
                        elif j:
                            break
                        if dock_rows(B, c, [(k1, tips[0], ext[2] if ext else port)], o1, kmin=4, clr=1.5) > 0:
                            docked = True
                            break
                        if ext:
                            c.wires.remove(ext)
                            c.boxes = [b_ for b_ in c.boxes if b_ != wbox(port, ext[2])]
                            c.terms = [t1 if (t[0] == k1 and t[1] == ext[2]) else t for t in c.terms]
            if not docked:
                B.attach(c, side, pref, port=port, gap=8 * U, clr=4.0)
        attach_stages(B, a)
    for a in anchors:                                       # further units of multi-unit parts
        pu = B._power_unit(a)
        for ui in range(1, len(D.units[a])):
            c = B._child()
            place_unit(c, a, ui, (0.0, 0.0))
            if ui == pu and a == prim:
                B.add_banks(c, a, unit=ui, side="R")
                pb, cb = B.bbox(), c.bbox()
                B.attach(c, "L", pref=(pb[0], pb[3]), port=(cb[2], cb[3]), gap=8 * U)
            elif not _chain_unit(B, c):
                B.attach(c, "B", gap=6 * U)
    release()
    tap_deferred(B)
    placed = {s.ref for s in B.syms}
    for m in ST.all_matches(B.sm):                          # a stage whose host never came up: back to the pool
        if any(r not in placed for r in m.refs()) and any(r in B.used for r in m.refs() if r not in placed):
            _drop(B, m, "its host was not drawn")
    left = [r for r in B.sat_refs if r not in B.used]

    def rail_head(r):
        ns = list(D.pins_of[r].values())
        return len(ns) == 2 and sum(1 for k in ns if D.is_rail(k) and not D.is_gnd(k)) == 1 and \
            sum(1 for k in ns if not D.is_rail(k)) == 1

    def lkey(r):
        side, pref, _k = B.dock_target(r)
        return (pref is None, D.npins(r) == 2 and not rail_head(r), side, round(pref[1] if pref else 0, 1),
                round(pref[0] if pref else 0, 1), _refkey(r))
    left.sort(key=lkey)
    if NODE_ON_WIRE:
        dock_nodes(B, left)
        dock_points(B, left)

    def pull_rail(r):
        ns = list(D.pins_of[r].values())
        if len(ns) != 2 or len(D.units.get(r, ())) != 1 or not r.startswith("R"):
            return None
        up = [k for k in ns if D.is_rail(k) and not D.is_gnd(k)]
        sg = [k for k in ns if D.conn(k) and not D.is_rail(k)]
        return up[0] if len(up) == 1 and len(sg) == 1 and D.named(sg[0]) else None
    for r in left:
        if r in B.used:
            continue
        side, pref, k = B.dock_target(r)
        out = DIRS[side]
        c = B.sub_cell(r, port=(k, out) if k is not None else None)
        done = False
        if k is not None and pref is not None:
            ports = [t[1] for t in c.terms if t[0] == k and tuple(t[2]) == (-out[0], -out[1])]
            tips = sorted((t[1] for t in B.terms if t[0] == k and tuple(t[2]) == out),
                          key=lambda q: abs(q[0] - pref[0]) + abs(q[1] - pref[1]))
            for tip in tips[:3]:
                if ports and dock_rows(B, c, [(k, tip, ports[0])], out, clr=1.5) > 0:
                    done = True
                    break
        if not done:
            port = next((t[1] for t in c.terms if t[0] == k), None) if k else None
            if k is not None and pref is not None and D.npins(r) > 2:
                ts = [t[1] for t in B.terms if t[0] == k]
                if ts:
                    pref = min(ts, key=lambda q: {"L": q[0], "R": -q[0], "T": q[1], "B": -q[1]}[side])
            if pull_rail(r) is not None:
                # pull-ups that found no room at their pins (I2C on a dense pin column) stand TOGETHER: one row,
                # one pitch, their names in one line below - not each wherever it happened to fit
                for r2 in left:
                    if r2 in B.used or pull_rail(r2) != pull_rail(r):
                        continue
                    c2 = B.sub_cell(r2)
                    cb, sb = c.bbox(), c2.bbox()
                    c2.shift(snap(cb[2] - sb[0] + 2 * U, 2 * U), 0.0)
                    c.absorb(c2)
                    B.fallback.append(r2)
            B.attach(c, side, pref, port=port, grow_weight=0.3 if D.npins(r) > 2 and pref is not None else 1.0)
        for ui in range(1, len(D.units[r])):
            c2 = B._child()
            c2.place(D.units[r][ui], (0.0, 0.0))
            B.attach(c2, "B", gap=6 * U)
        B.fallback.append(r)
