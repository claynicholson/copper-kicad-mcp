"""Idiom data model: a Match says WHICH parts form a known sub-circuit, a Template says HOW it is drawn.

A template is rigid geometry in sheet millimetres (1.27 grid): where one pin of every part lands and which way it
points, the wires as polylines per net role, the open ends (ports), power symbols (supplies up, grounds down - no
other orientation exists here) and label slots. `instantiate` poses the real symbols from their real pin geometry
and then PROVES the drawing against the netlist before anything is handed to the block:

  * every wire is orthogonal and touches only pins of its own net (template parts and the pins of the context),
  * wires of two different nets never share a point,
  * every connected pin of every template part is reached by a wire, a power symbol or a port of its net,
  * a power symbol sits on a rail, 'down' exactly when that rail is a ground.

Any failure raises IdiomError: the match is dropped and its parts are drawn the legacy way (stub + label). A wrong
match can therefore give an odd drawing, never a wrong netlist.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..build import (U, hit, inline_fields, lbl_box, pins, place_fields, pose_pin, power_box, side_fields, snap,
                     sym_boxes, wbox)

IDIOMS = ("decap", "modgnd", "led", "divider", "button", "rc", "pull",
          "opamp", "boost", "buck", "ldo", "crystal", "usbc", "tswitch", "peer")


class IdiomError(Exception):
    pass


@dataclass
class Match:
    idiom: str                                   # 'decap', 'divider', 'rc', 'led', 'button', 'pull', 'modgnd'
    roles: dict                                  # role -> ref
    nets: dict                                   # net role -> net key
    host: tuple | None = None                    # (ref, pin number) it docks on; None = a stage of its own
    score: int = 0                               # priority (higher claims first)
    extra: dict = field(default_factory=dict)    # matcher notes for the template builder (orders, directions)

    def refs(self) -> list:
        return list(self.roles.values())


@dataclass
class Template:
    parts: dict = field(default_factory=dict)    # role -> (pin number, (x, y), out, field slot[, mirror])
    wires: list = field(default_factory=list)    # [(net role, [(x, y), ...])]
    ports: dict = field(default_factory=dict)    # net role -> (x, y, out): open end, the net goes on from here
    powers: list = field(default_factory=list)   # [(net role, (x, y), 'up' | 'down')]
    labels: dict = field(default_factory=dict)   # net role -> (x, y, out): where the net's name may stand
    flags: dict = field(default_factory=dict)    # net role -> (x, y): slot for a PWR_FLAG of that net
    flow: tuple = ()                             # (in port, out port)
    cross: bool = False                          # two of its nets may cross squarely (never touch end-on)
    through: dict = field(default_factory=dict)  # builder's note: x of a doubled-contact column -> x of its far contact


def fields_left(si):
    """Vertical 2-pin part: Reference over Value, right-aligned just LEFT of the body."""
    side_fields(si)
    from .... import sexpr
    from ...model import _is_hidden, num, text_width
    from ..score import graphic_box
    gb = graphic_box(si.sch, si)
    for p in sexpr.find_all(si.node, "property"):
        if len(p) > 2 and not _is_hidden(p) and str(p[2]):
            a = sexpr.find(p, "at")
            a[1] = num(round(gb[0] - 1.0 - text_width(str(p[2])) / 2, 3))


def fields_below(si):
    """Horizontal 2-pin part: Reference and Value side by side BELOW the body (the row above is taken)."""
    inline_fields(si)
    from .... import sexpr
    from ...model import _is_hidden, num
    from ..score import graphic_box
    gb = graphic_box(si.sch, si)
    for p in sexpr.find_all(si.node, "property"):
        if len(p) > 2 and not _is_hidden(p) and str(p[2]):
            a = sexpr.find(p, "at")
            a[2] = num(round(gb[1] + gb[3] - float(a[2]), 3))


def _crosses(a, b, c, d) -> bool:
    """Two orthogonal segments cross squarely, each in the INTERIOR of the other."""
    if abs(a[1] - b[1]) < 1e-6 and abs(c[0] - d[0]) < 1e-6:
        h, v = (a, b), (c, d)
    elif abs(a[0] - b[0]) < 1e-6 and abs(c[1] - d[1]) < 1e-6:
        h, v = (c, d), (a, b)
    else:
        return False
    x, y = v[0][0], h[0][1]
    return min(h[0][0], h[1][0]) + 2.5 < x < max(h[0][0], h[1][0]) - 2.5 and \
        min(v[0][1], v[1][1]) + 2.5 < y < max(v[0][1], v[1][1]) - 2.5


def _on_seg(p, a, b) -> bool:
    return min(a[0], b[0]) - 1e-6 <= p[0] <= max(a[0], b[0]) + 1e-6 and min(a[1], b[1]) - 1e-6 <= p[1] <= max(a[1], b[1]) + 1e-6 \
        and (abs(a[0] - b[0]) < 1e-6 and abs(p[0] - a[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6 and abs(p[1] - a[1]) < 1e-6)


def _segs_touch(a, b, c, d) -> bool:
    """Two orthogonal segments share at least one point."""
    ax0, ax1, ay0, ay1 = min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])
    cx0, cx1, cy0, cy1 = min(c[0], d[0]), max(c[0], d[0]), min(c[1], d[1]), max(c[1], d[1])
    return ax0 <= cx1 + 1e-6 and cx0 <= ax1 + 1e-6 and ay0 <= cy1 + 1e-6 and cy0 <= ay1 + 1e-6


def instantiate(D, m: Match, t: Template, ctx_pins: dict | None = None) -> dict:
    """Pose the parts of `m` as `t` says and check the result. -> piece for Blk:
    {'wires', 'powers', 'boxes', 'terms', 'resv', 'used', 'syms'}.  Raises IdiomError."""
    net = {}
    for role, key in m.nets.items():
        if key is None or key not in D.view.nets:
            raise IdiomError(f"{m.idiom}: net role {role} has no net")
        net[role] = key
    syms, tips = [], {}
    for role, spec in t.parts.items():
        pin_no, xy, out, slot = spec[:4]
        mirror = spec[4] if len(spec) > 4 else None
        ref = m.roles.get(role)
        if ref is None or ref not in D.units or len(D.units[ref]) != 1:
            raise IdiomError(f"{m.idiom}: role {role} has no single-unit part")
        si = D.units[ref][0]
        if not pose_pin(si, pin_no, (snap(xy[0]), snap(xy[1])), tuple(out), mirror):
            raise IdiomError(f"{m.idiom}: {ref} has no pose with pin {pin_no} pointing {out}")
        if slot == "left":
            fields_left(si)
        elif slot == "right":
            side_fields(si)
        elif slot == "below":
            fields_below(si)
        elif slot in ("twin", "twinl"):
            from .passive import twin_fields
            twin_fields(si, slot == "twinl")
        else:
            place_fields(si)
        syms.append(si)
        for no, p in pins(si).items():
            pt = (snap(p["xy"][0]), snap(p["xy"][1]))
            k = D.pins_of[ref].get(no)
            if pt in tips and tips[pt][0] != k:
                raise IdiomError(f"{m.idiom}: pins of two nets coincide at {pt}")
            tips[pt] = (k, ref, no)
    if set(m.roles) - set(t.parts):
        raise IdiomError(f"{m.idiom}: template leaves {sorted(set(m.roles) - set(t.parts))} undrawn")
    ctx = {(snap(p[0]), snap(p[1])): k for p, k in (ctx_pins or {}).items()}
    segs = []
    for role, pts in t.wires:
        if role not in net:
            raise IdiomError(f"{m.idiom}: wire on unknown net role {role}")
        pts = [(snap(p[0]), snap(p[1])) for p in pts]
        for a, b in zip(pts, pts[1:]):
            if a == b:
                continue
            if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6:
                raise IdiomError(f"{m.idiom}: diagonal wire {a}-{b}")
            segs.append((net[role], a, b))
    for k, a, b in segs:                                   # per-wire pin_net assertion
        for pt, (pk, ref, no) in tips.items():
            if pk != k and _on_seg(pt, a, b):
                raise IdiomError(f"{m.idiom}: wire of {D.text(k) or k} touches {ref}.{no}")
        for pt, pk in ctx.items():
            if pk != k and _on_seg(pt, a, b):
                raise IdiomError(f"{m.idiom}: wire of {D.text(k) or k} touches a pin of another net at {pt}")
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if segs[i][0] != segs[j][0] and _segs_touch(segs[i][1], segs[i][2], segs[j][1], segs[j][2]) \
                    and not (t.cross and _crosses(segs[i][1], segs[i][2], segs[j][1], segs[j][2])):
                raise IdiomError(f"{m.idiom}: wires of {segs[i][0]} and {segs[j][0]} touch")
    powers = []
    for role, pt, orient in t.powers:
        k = net.get(role)
        if k is None or not D.is_rail(k):
            raise IdiomError(f"{m.idiom}: power symbol on a net that is no rail ({role})")
        if orient not in ("up", "down") or (orient == "down") != D.is_gnd(k):
            raise IdiomError(f"{m.idiom}: power symbol of {D.text(k)} drawn '{orient}'")
        pt = (snap(pt[0]), snap(pt[1]))
        for k2, a, b in segs:
            if k2 != k and _on_seg(pt, a, b):
                raise IdiomError(f"{m.idiom}: power symbol of {D.text(k)} on a wire of {D.text(k2) or k2}")
        if pt in tips and tips[pt][0] != k:
            raise IdiomError(f"{m.idiom}: power symbol of {D.text(k)} on a foreign pin")
        powers.append((k, pt, orient, D.text(k)))
    terms, resv = [], []
    for src in (t.ports, t.labels):
        for role, (x, y, out) in src.items():
            k = net.get(role)
            if k is None:
                raise IdiomError(f"{m.idiom}: port on unknown net role {role}")
            pt = (snap(x), snap(y))
            if not any(k2 == k and _on_seg(pt, a, b) for k2, a, b in segs) and tips.get(pt, (None,))[0] != k:
                raise IdiomError(f"{m.idiom}: port of {D.text(k) or k} is not on its wire")
            if D.is_rail(k):
                continue
            terms.append((k, pt, tuple(out)))
            if D.named(k):
                resv.append((k, lbl_box(D.text(k), pt, tuple(out), D.label_kind(k))))
    reached = set()
    for pt, (pk, ref, no) in tips.items():                 # every connected pin is reached by its own net
        if not D.conn(pk):
            continue
        ok = any(k == pk and _on_seg(pt, a, b) for k, a, b in segs) or any(k == pk and q == pt for k, q, _o, _v in powers) \
            or any(k == pk and q == pt for k, q, _o in terms)
        if not ok:
            raise IdiomError(f"{m.idiom}: {ref}.{no} is left unconnected by the template")
        reached.add((ref, no))
    boxes = []
    per = []
    for si in syms:
        b = sym_boxes(si)
        per.append(b)
        boxes += b
    for i in range(len(per)):
        for j in range(i + 1, len(per)):
            if any(hit(a, b, 0.05) for a in per[i] for b in per[j]):
                raise IdiomError(f"{m.idiom}: parts overlap ({syms[i].ref}, {syms[j].ref})")
    pboxes = [power_box(v, q, o) for _k, q, o, v in powers]
    for pb in pboxes:
        if any(hit(pb, b, 0.05) for bs in per for b in bs):
            raise IdiomError(f"{m.idiom}: a power symbol overlaps a part")
    for k, a, b in segs:                                   # a wire never runs through a body or a text of the idiom
        wb = wbox(a, b, 0.02)
        for si, bs in zip(syms, per):
            mine = [tuple(pt) for pt, (_pk, ref, _no) in tips.items() if ref == si.ref]
            for bx in bs[1:]:
                if hit(wb, bx, 0.1):
                    raise IdiomError(f"{m.idiom}: a wire runs through the text of {si.ref}")
            body = bs[0]
            inner = [body[0] + 0.6, body[1] + 0.6, body[2] - 0.6, body[3] - 0.6]
            if inner[0] < inner[2] and inner[1] < inner[3] and hit(wb, inner, 0.0) and not any(_on_seg(q, a, b) for q in mine):
                raise IdiomError(f"{m.idiom}: a wire runs through {si.ref}")
    return {"wires": segs, "powers": powers, "boxes": boxes + pboxes, "terms": terms, "resv": resv,
            "used": [m.roles[r] for r in t.parts], "syms": syms}


__all__ = ["IDIOMS", "IdiomError", "Match", "Template", "instantiate", "fields_left", "U"]
