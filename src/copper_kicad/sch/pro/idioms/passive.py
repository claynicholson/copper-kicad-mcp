"""Templates of the passive idioms: decoupling at the pin, divider, RC, LED + resistor, button, pull-up / -down.

Every builder is a pure function (Design, Match, anchor geometry) -> Template. Poses come from the real pin
geometry of the symbols (the parts are posed while the template is built to read their far pin and their extent);
`model.instantiate` then re-poses them and proves the drawing. Signal flows left to right; supplies point up and
grounds down - there is no sideways power symbol in any template.
"""
from __future__ import annotations

import math

from ...model import text_width
from ..build import GAP, STUB, U, hit, pins, pose_pin, power_box, side_fields, snap, sym_boxes
from .match import ends
from .model import IdiomError, Match, Template, fields_left


def _hang(T: Template, D, role, ref, near_net, x, y, sg, lead=GAP, slot="auto", near_role=None, d=0):
    """Part `ref` vertical, its pin on `near_net` at (x, y + sg*lead), body away from the row (sg +1 = below).
    -> (far pin xy, boxes, far net)"""
    pn, pf, M = ends(D, ref, near_net)
    si = D.units[ref][0]
    tgt = (snap(x), snap(y + sg * lead))
    if len(D.pins_of[ref]) != 2:
        return _hang_twin(T, D, role, ref, near_net, tgt, sg, slot, near_role, d)
    if not pose_pin(si, pn, tgt, (0, -sg)):
        raise IdiomError(f"{ref} cannot stand upright")
    if slot == "left":
        fields_left(si)
    elif slot == "right":
        side_fields(si)
    T.parts[role] = (pn, tgt, (0, -sg), slot)
    far = pins(si)[pf]["xy"]
    return (snap(far[0]), snap(far[1])), sym_boxes(si), M


def _hang_twin(T: Template, D, role, ref, near_net, tgt, sg, slot, near_role, d=0):
    """(package B) A part whose contacts are doubled inside (pins 1 / 2 and 3 / 4 of a tactile switch) hangs on the
    wire like the two-terminal part it is:

        ──●──────          the wire comes down ON the tip of one contact of the near net; the contact of the far
          │                net that lies straight beyond it goes on to the supply / ground symbol. The twin of
          ├1─┬─2┤          either contact is joined by the SHORTEST STRAIGHT LINK: tip to tip along the pin row,
            /              on the line the symbol itself draws between the two pins. No bar over the part, no ring
          ├3─┴─4┤          around it, one ground symbol.
          ⏚
    -> (far pin xy, boxes, far net)"""
    if near_role is None:
        raise IdiomError(f"{ref}: a part with doubled pins cannot stand here")
    si = D.units[ref][0]
    po = D.pins_of[ref]
    near = sorted(p for p, k in po.items() if k == near_net)
    best = None
    # the pin looks back along the wire = the body stands beyond the drop, away from the pin the wire comes from
    side = (1, -1) if (slot == "left" or d < 0) else (-1, 1)
    for ox in side:
        for mirror in (None, "x", "y"):
            for pn in near:
                if not pose_pin(si, pn, tgt, (ox, 0), mirror):
                    continue
                pw = {no: ((snap(q["xy"][0]), snap(q["xy"][1])), tuple(q["out"])) for no, q in pins(si).items()}
                if set(pw) != set(po):
                    continue
                far = [no for no, k in po.items() if k != near_net and pw[no][1] == (ox, 0)
                       and abs(pw[no][0][0] - tgt[0]) < 0.01 and (pw[no][0][1] - tgt[1]) * sg > 0.01]
                links = []
                for k in set(po.values()):
                    ps = sorted((pw[no] for no, kk in po.items() if kk == k))
                    if len(ps) == 1:
                        continue
                    (a, oa), (b, ob) = ps
                    if not (len(ps) == 2 and abs(a[1] - b[1]) < 0.01 and oa == (-1, 0) and ob == (1, 0)):
                        links = None                       # the twins do not face each other across one row
                        break
                    links.append((k, a, b))
                if not far or links is None:
                    continue
                best = (pn, (ox, 0), mirror, far[0], links)
                break
            if best:
                break
        if best:
            break
    if best is None:
        raise IdiomError(f"{ref}: no pose with one contact above the other")
    pn, out, mirror, pf, links = best
    pose_pin(si, pn, tgt, out, mirror)
    T.parts[role] = (pn, tgt, out, "twin" if out[0] < 0 else "twinl", mirror)
    for k, a, b in links:
        T.wires.append((near_role if k == near_net else role, [a, b]))
        if k == near_net:
            # the net goes on from the far contact (the part is a piece of the wire)
            T.through[tgt[0]] = (b if abs(a[0] - tgt[0]) < 0.01 else a)[0]
    fq = pins(si)[pf]["xy"]
    twin_fields(si, out[0] > 0)
    return (snap(fq[0]), snap(fq[1])), sym_boxes(si), po[pf]


def twin_fields(si, left=False):
    """Reference over Value beside the body, on the side away from the drop, between the two contact rows."""
    from .... import sexpr
    from ...model import _is_hidden, _set_justify, num, text_width
    ps = pins(si)
    xs = [q["xy"][0] for q in ps.values()]
    ys = sorted({round(q["xy"][1], 2) for q in ps.values()})
    yc = (ys[0] + ys[-1]) / 2
    xr = max(xs) + 1.0
    vis = [p for p in sexpr.find_all(si.node, "property") if len(p) > 2 and not _is_hidden(p) and str(p[2])
           and sexpr.find(p, "at") is not None]
    for i, p in enumerate(vis):
        at = sexpr.find(p, "at")
        yy = yc + (i - (len(vis) - 1) / 2) * 2.2
        w = text_width(str(p[2])) / 2
        at[1:] = [num(round(min(xs) - 1.0 - w if left else xr + w, 3)), num(round(yy, 3)), 0]
        _set_justify(p, None)


def _inline(T: Template, D, role, ref, near_net, x, y, d):
    """Part `ref` lying in the row, its pin on `near_net` at (x, y), body towards d. -> (far xy, boxes, far net)"""
    pn, pf, M = ends(D, ref, near_net)
    si = D.units[ref][0]
    if not pose_pin(si, pn, (snap(x), snap(y)), (-d, 0)):
        raise IdiomError(f"{ref} cannot lie in the row")
    T.parts[role] = (pn, (snap(x), snap(y)), (-d, 0), "auto")
    far = pins(si)[pf]["xy"]
    return (snap(far[0]), snap(far[1])), sym_boxes(si), M


def _label_room(D, k) -> int:
    """Grid steps (of U) a pin wire needs before the first part so that the net's name fits on it."""
    return max(4, 2 * math.ceil((text_width(D.text(k)) + 4.0) / (2 * U))) if D.named(k) else 4


def _roles(m: Match, pref: str) -> list:
    return sorted((r for r in m.roles if r.startswith(pref) and r[len(pref):].isdigit()), key=lambda r: int(r[len(pref):]))


def _columns(T: Template, D, m: Match, pref: str, net_role: str, x, y, d, taken: list):
    """Parts that hang on one node: column i holds the i-th part to a supply (standing up, supply symbol on top)
    and the i-th part to ground (hanging down, ground symbol below). A divider is one column, its filter cap the
    next. -> (x of the last column or None, x for whatever comes next)"""
    ups, downs = _roles(m, pref + "up"), _roles(m, pref + "down")
    N = m.nets[net_role]
    last = None
    for i in range(max(len(ups), len(downs))):
        for _attempt in range(16):
            col, pend = [], []
            for role, sg in ((ups[i] if i < len(ups) else None, -1), (downs[i] if i < len(downs) else None, 1)):
                if role is None:
                    continue
                # the part stands with its pin ON the row (a junction dot on the wire, no 2.54 mm jog that would
                # put its pin half a row off the series part next to it)
                far, bx, M = _hang(T, D, role, m.roles[role], N, x, y, sg, lead=0, near_role=net_role, d=d)
                orient = "up" if sg < 0 else "down"
                q = (far[0], snap(far[1] + sg * U))
                col += bx + [power_box(D.text(M), q, orient)]
                pend.append((role, sg, far, q, orient))
            if not any(hit(a, b, -0.6) for a in col for b in taken):
                break
            x = snap(x + d * 2 * U)
        for role, sg, far, q, orient in pend:
            T.wires.append((role, [far, q]))
            T.powers.append((role, q, orient))
        taken += col
        last = x
        x = snap(x + d * 3 * 2 * U)
    return last, x


def node_template(D, m: Match, tip, d: int, k: int):
    """pull / divider / button / RC on the wire that leaves a side pin at `tip` in direction d (+1 right, -1 left):

        pin ──┬──────┬── NAME          pin ── NAME ──┬──[ R ]──┬── NAME2
             [R]    [C]                              [C]        [C]
              ⏚      ⏚                                ⏚          ⏚
    Returns None when k leaves no room for the net name between the pin and the first part."""
    T = Template()
    px, py = tip
    N = m.nets["N"]
    taken: list = []
    has_series = "series" in m.roles
    if has_series and k < _label_room(D, N):
        return None
    last, nx = _columns(T, D, m, "", "N", snap(px + d * k * U), py, d, taken)
    if has_series:
        xs = nx if last is not None else snap(px + d * k * U)
        for _attempt in range(12):
            far, bx, M = _inline(T, D, "series", m.roles["series"], N, xs, py, d)
            if not any(hit(a, b, -0.6) for a in bx for b in taken):
                break
            xs = snap(xs + d * 2 * U)
        taken += bx
        T.wires.append(("N", [(px, py), (xs, py)]))
        T.labels["N"] = (snap(px + d * U), py, (d, 0))
        if _roles(m, "tup") or _roles(m, "tdown"):
            room = 4
            if any(len(D.pins_of[m.roles[r]]) != 2 for r in _roles(m, "tup") + _roles(m, "tdown")):
                room = max(4, _label_room(D, m.nets["M"]))     # no wire beyond a part with doubled contacts: the name stands here
            tlast, _ = _columns(T, D, m, "t", "M", snap(far[0] + d * room * U), py, d, taken)
            end = (snap(tlast + d * STUB), py)
            if tlast in T.through:
                end = (T.through[tlast], py)
        else:
            end = (snap(far[0] + d * STUB), py)
        T.wires.append(("M", [far, end]))
        T.ports["M"] = (end[0], end[1], (d, 0))
        T.flow = ("N", "M") if d > 0 else ("M", "N")
    else:
        end = (snap(last + d * STUB), py)
        if last in T.through:
            end = (T.through[last], py)
        T.wires.append(("N", [(px, py), end]))
        T.ports["N"] = (end[0], end[1], (d, 0))
    return T


def pull_inline_template(D, m: Match, tip, d: int, k: int, rows: int):
    """A lone pull-down on a pin whose LOWER neighbours carry signals too (CC of a USB-C receptacle right above
    D+ / D-): the resistor lies in the pin's row and its ground drops beyond it, `rows` pin pitches down, so the rows
    below stay open for the straight wires of those pins (a resistor hanging through them blocks every one):

        pin ── NAME ──[ R ]──┐
        pin ─────────────────┼──
                             ⏚
    Returns None when the match is not that, or k leaves no room for the net name."""
    if set(m.roles) != {"down0"} or k < _label_room(D, m.nets["N"]):
        return None
    T = Template()
    px, py = tip
    xs = snap(px + d * k * U)
    far, _bx, _M = _inline(T, D, "down0", m.roles["down0"], m.nets["N"], xs, py, d)
    T.wires.append(("N", [(px, py), (xs, py)]))
    T.labels["N"] = (snap(px + d * U), py, (d, 0))
    xb = snap(far[0] + d * 2 * U)
    q = (xb, snap(py + rows * 2 * U + GAP))
    T.wires.append(("down0", [far, (xb, far[1]), q]))
    T.powers.append(("down0", q, "down"))
    return T


def led_template(D, m: Match, tip, d: int, k: int):
    """LED + series resistor between a side pin and a rail, drawn in the direction the current flows:
    in the row when that reads left to right (supply at the far left / ground at the far right), otherwise as a
    column - supply on top, the chain coming down into the pin's wire (or going down to ground).
    Roles e1 (on the pin's net N), e2 (from the middle net M to the rail END)."""
    T = Template()
    px, py = tip
    N, M = m.nets["N"], m.nets["M"]
    end_up = m.extra.get("end") == "up"
    if k < _label_room(D, N):
        return None
    x = snap(px + d * k * U)
    T.labels["N"] = (snap(px + d * U), py, (d, 0))
    if (d < 0) == end_up:
        far1, bx1, _ = _inline(T, D, "e1", m.roles["e1"], N, x, py, d)
        g = (2 * math.ceil((text_width(D.text(M)) + 3.0) / (2 * U)) if D.named(M) else 2) * U
        near2 = (snap(far1[0] + d * g), py)
        for _attempt in range(8):
            far2, bx2, _ = _inline(T, D, "e2", m.roles["e2"], M, near2[0], py, d)
            if not any(hit(a, b, -0.4) for a in bx1 for b in bx2):
                break
            near2 = (snap(near2[0] + d * 2 * U), py)
        T.wires += [("N", [(px, py), (x, py)]), ("M", [far1, near2])]
        if D.conn(M) and (D.named(M) or len(D.view.nets[M].pins) > 2):
            T.labels["M"] = (min(far1[0], near2[0]), py, (1, 0))
        for extra in range(0, 14, 2):                      # the rail symbol stands clear of the last part's texts
            e = (snap(far2[0] + d * (STUB + extra * U)), py)
            q = (e[0], snap(py + (-GAP if end_up else GAP)))
            pb = power_box(D.text(m.nets["END"]), q, "up" if end_up else "down")
            if not any(hit(pb, b, 0.0) for b in bx1 + bx2):
                break
        T.wires.append(("END", [far2, e, q]))
        T.powers.append(("END", q, "up" if end_up else "down"))
        T.flow = ("END", "N") if end_up else ("N", "END")
        return T
    sg = -1 if end_up else 1
    far1, bx1, _ = _hang(T, D, "e1", m.roles["e1"], N, x, py, sg)
    g = 4 * U if D.named(M) else 2 * U
    far2, bx2, _ = _hang(T, D, "e2", m.roles["e2"], M, far1[0], far1[1], sg, lead=g)
    near2 = (far1[0], snap(far1[1] + sg * g))
    T.wires += [("N", [(px, py), (x, py), (x, snap(py + sg * GAP))]), ("M", [far1, near2])]
    if D.conn(M) and (D.named(M) or len(D.view.nets[M].pins) > 2):
        T.labels["M"] = (far1[0], snap(far1[1] + sg * g / 2), (-1, 0))
    q = (far2[0], snap(far2[1] + sg * U))
    T.wires.append(("END", [far2, q]))
    T.powers.append(("END", q, "up" if end_up else "down"))
    T.flow = ("END", "N") if end_up else ("N", "END")
    return T


def _pitch(bx, x, s) -> float:
    ext = (max(b[2] for b in bx) - x) if s > 0 else (x - min(b[0] for b in bx))
    return max(3, math.ceil((ext + 0.8 + 2.3) / (2 * U))) * 2 * U


def _grounds(T: Template, fars: list):
    """One ground symbol per row of caps: a ground bar joins caps that end on one line."""
    if not fars:
        return
    ys = {f[1] for f in fars}
    if len(fars) > 1 and len(ys) == 1:
        yg = snap(fars[0][1] + U)
        for f in fars:
            T.wires.append(("GND", [f, (f[0], yg)]))
        xs = sorted(f[0] for f in fars)
        T.wires.append(("GND", [(xs[0], yg), (xs[-1], yg)]))
        T.powers.append(("GND", (fars[-1][0], yg), "down"))     # at the outermost cap: clear of the part
    else:
        for f in fars:
            q = (f[0], snap(f[1] + U))
            T.wires.append(("GND", [f, q]))
            T.powers.append(("GND", q, "down"))


def decap_template(D, m: Match, pts: list, out, k: int, side: str = "R"):
    """Decoupling caps of `m` drawn AT the supply pin(s) `pts` (tips of ONE rail on ONE side of the part).

    side pin (out = (±1, 0)):  pin ──┬────┬─ +3V3↑       top pin (out = (0, -1)):   +3V3↑
                                    [C]  [C]                                   ┌──┬──┐
                                     ⏚────┘                                   [C] │ [C]
                                                                               ⏚ pin  ⏚
    The supply symbol stands on the outermost cap (a T, never a four-way junction); `k` = distance of the cap row
    from the pin tip in grid steps; `side` = 'R' | 'L' | 'LR': where the caps go next to a top / bottom pin."""
    T = Template()
    rail = m.nets["RAIL"]
    roles = _roles(m, "c")
    if not roles or not pts:
        raise IdiomError("decap: nothing to draw")
    out = tuple(out)
    if out[1] == 0:
        d = out[0]
        ys = sorted(p[1] for p in pts)
        px = pts[0][0]
        bx_ = snap(px + d * k * U)
        for y in ys:
            T.wires.append(("RAIL", [(px, y), (bx_, y)]))
        if len(ys) > 1:
            T.wires.append(("RAIL", [(bx_, ys[0]), (bx_, ys[-1])]))
        x, xs, fars = bx_, [], []
        for role in roles:
            far, bx, _ = _hang(T, D, role, m.roles[role], rail, x, ys[-1], 1, lead=0, slot="right" if d > 0 else "left")
            xs.append(x)
            fars.append(far)
            x = snap(x + d * _pitch(bx, x, d))
        if len(xs) > 1:
            T.wires.append(("RAIL", [(xs[0], ys[-1]), (xs[-1], ys[-1])]))
        T.powers.append(("RAIL", (xs[-1], ys[0] if xs[-1] == bx_ else ys[-1]), "up"))
        _grounds(T, fars)
        return T
    if out == (0, -1):
        xp = sorted(p[0] for p in pts)
        py = pts[0][1]
        yb = snap(py - k * U)
        for x in xp:
            T.wires.append(("RAIL", [(x, py), (x, yb)]))
        seq = {"R": [1] * len(roles), "L": [-1] * len(roles)}.get(side) or [1 if i % 2 == 0 else -1 for i in range(len(roles))]
        nxt = {1: snap(xp[-1] + 3 * U), -1: snap(xp[0] - 3 * U)}
        placed = {1: [], -1: []}
        for role, s in zip(roles, seq):
            x = nxt[s]
            far, bx, _ = _hang(T, D, role, m.roles[role], rail, x, yb, 1, lead=0, slot="right" if s > 0 else "left")
            placed[s].append((x, far))
            nxt[s] = snap(x + s * _pitch(bx, x, s))
        allx = xp + [x for s in placed for x, _f in placed[s]]
        T.wires.append(("RAIL", [(min(allx), yb), (max(allx), yb)]))
        first = seq[0]
        T.powers.append(("RAIL", (placed[first][-1][0], yb), "up"))
        for s in (1, -1):
            _grounds(T, [f for _x, f in placed[s]])
        return T
    # bottom pin: the first cap hangs straight under the pin, the supply symbol stands beside it on a short hook
    if len(pts) != 1:
        raise IdiomError("decap: several bottom pins")
    px, py = pts[0]
    s = -1 if side == "L" else 1
    yn = snap(py + k * U)
    T.wires.append(("RAIL", [(px, py), (px, yn)]))
    x, xs, fars = px, [], []
    for role in roles:
        far, bx, _ = _hang(T, D, role, m.roles[role], rail, x, yn, 1, lead=0, slot="right" if s > 0 else "left")
        xs.append(x)
        fars.append(far)
        x = snap(x + s * _pitch(bx, x, s))
    hook = snap(xs[-1] + s * 4 * U) if len(xs) == 1 else xs[-1]
    if hook != px:
        T.wires.append(("RAIL", [(px, yn), (hook, yn)]))
    T.powers.append(("RAIL", (hook, yn), "up"))
    _grounds(T, fars)
    return T


BUS_ROW = 6                # class D: caps side by side in one row of a rail bar; more wrap into a second row above


def bus_rows(roles: list, side: str) -> list:
    """How the caps of one rail bar are dealt into rows: [(direction +1 right / -1 left, [roles])]. One side gets
    rows of at most BUS_ROW (the first row hangs on the bar, the next ones stand above it); 'LR' / 'RL' gives every
    second cap to the other side."""
    def one(s, rs):
        if not rs:
            return []
        nrow = math.ceil(len(rs) / BUS_ROW)
        per = math.ceil(len(rs) / nrow)
        return [(s, rs[i * per:(i + 1) * per]) for i in range(nrow)]
    if side in ("R", "L"):
        return one(1 if side == "R" else -1, list(roles))
    s0 = 1 if side == "RL" else -1
    return one(s0, list(roles[0::2])) + one(-s0, list(roles[1::2]))


def rail_bus_template(D, m: Match, pts: list, k: int, rows: list):
    """Class D: ONE rail bar for the supply pins `pts` of one rail on top of a part (tips on one line), and ALL the
    decoupling caps of that rail on it - every cap visibly belongs to the pins the bar is wired to.

                 +3V3
                  │
            ┌──┬──┼──┬──────┬──┐          risers from every pin up to the bar, `k` grid steps above the tips;
           [C][C][C] │      │  │          the bar runs on past the outermost pin, the caps hang DOWN from it on one
            ⏚──┴──┘ pin    pin pin        pitch (smallest value nearest the pins), one ground bar and ONE ground
                                          symbol per row, ONE supply symbol on the outer end of the bar.
    `rows` = [(direction, [roles])] (bus_rows). A second row on the same side stands above the first on a bar of
    its own, joined to the first bar by one vertical wire at the outer end; the supply symbol then stands on top of
    that wire. Its caps stand in the columns of the row below."""
    T = Template()
    rail = m.nets["RAIL"]
    if not pts or not rows or not any(rs for _s, rs in rows):
        raise IdiomError("decap: nothing to draw")
    if len({snap(p[1]) for p in pts}) != 1:
        raise IdiomError("decap: the pins of a rail bar are not on one line")
    xp = sorted(snap(p[0]) for p in pts)
    py = snap(pts[0][1])
    yb = snap(py - k * U)
    for x in xp:
        T.wires.append(("RAIL", [(x, py), (x, yb)]))
    gh = power_box(D.text(m.nets["GND"]), (0.0, 0.0), "down")[3]
    lo, hi = xp[0], xp[-1]
    cols: dict = {}                      # direction -> columns of the first row, outer end, bar height, tiers
    sym, sym_side = None, 0
    for s, roles in rows:
        if not roles:
            continue
        if s not in cols:
            x, xs, fars = snap((hi if s > 0 else lo) + s * 3 * U), [], []
            for role in roles:
                far, bx, _ = _hang(T, D, role, m.roles[role], rail, x, yb, 1, lead=0, slot="right" if s > 0 else "left")
                xs.append(x)
                fars.append(far)
                x = snap(x + s * _pitch(bx, x, s))
            cols[s] = {"xs": xs, "link": x, "y": yb, "drop": fars[0][1] - yb, "end": xs[-1]}
            if s > 0:
                hi = xs[-1]
            else:
                lo = xs[-1]
            _grounds(T, fars)
            if sym is None:
                sym, sym_side = (xs[-1], yb), s
            continue
        c = cols[s]
        if len(roles) > len(c["xs"]):
            raise IdiomError("decap: an upper row is longer than the row below it")
        pitch = math.ceil((c["drop"] + U + gh + 1.5) / (2 * U)) * 2 * U
        y2 = snap(c["y"] - pitch)
        xl = c["link"]
        # the bar below runs on to the link, the link rises to the next bar
        T.wires.append(("RAIL", [(c["end"], c["y"]), (xl, c["y"]), (xl, y2)]))
        xs = c["xs"][len(c["xs"]) - len(roles):]
        fars = []
        for role, x in zip(roles, xs):
            far, _bx, _ = _hang(T, D, role, m.roles[role], rail, x, y2, 1, lead=0, slot="right" if s > 0 else "left")
            fars.append(far)
        T.wires.append(("RAIL", [(xl, y2), (xs[0], y2)]))
        _grounds(T, fars)
        if s == sym_side:
            sym = (xl, y2)                                  # the supply symbol stands on top of the link
        c["y"], c["end"] = y2, xl
    T.wires.append(("RAIL", [(lo, yb), (hi, yb)]))
    T.powers.append(("RAIL", sym, "up"))
    return T


def bank_template(D, m: Match):
    """Caps that found no room at a pin: one compact row under its own supply symbol, origin = first cap's top."""
    T = Template()
    rail = m.nets["RAIL"]
    x, xs, fars = 0.0, [], []
    for role in _roles(m, "c"):
        far, bx, _ = _hang(T, D, role, m.roles[role], rail, x, 0.0, 1, lead=0, slot="right")
        xs.append(x)
        fars.append(far)
        x = snap(x + _pitch(bx, x, 1))
    if not xs:
        raise IdiomError("decap: empty bank")
    if len(xs) > 1:
        T.wires.append(("RAIL", [(xs[0], 0.0), (xs[-1], 0.0)]))
    T.powers.append(("RAIL", (xs[-1], 0.0), "up"))
    _grounds(T, fars)
    return T
