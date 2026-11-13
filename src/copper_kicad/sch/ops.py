"""``sch_apply`` engine: validated, atomic batches of schematic operations.

All ops run against an in-memory copy of the sheet. Nothing touches disk
unless every op succeeded; then a checkpoint is taken, the file is written
atomically and KiCad re-saves it in canonical form.

Connectivity is pin-anchored: callers name pins (``U1.PA9``, ``J1.VBUS``,
``R1.2``) and Copper computes exact pin tips (rotation/mirror aware), draws a
2.54 mm stub outward and terminates it with a label or a power symbol.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from .. import sexpr
from ..sexpr import Sym
from ..mcp.registry import ToolError
from ..kicad import libs
from .. import project as P
from . import symbols as G
from . import layout as LAY
from .model import Schematic, SymInst, autoplace_fields, num, new_uuid, text_width

STUB = 2.54
EPS = 0.01

OP_FIELDS = {
    "place": {"lib_id", "lcsc", "part", "provider", "spec", "ref", "id", "value", "footprint", "at", "rot", "mirror",
              "unit", "fields", "group", "near", "dnp", "body_style"},
    "connect": {"pin", "pins", "net", "kind", "style"},
    "power": {"pin", "pins", "net"},
    "no_connect": {"pin", "pins"},
    "wire": {"from", "to", "points"},
    "label": {"net", "at", "kind", "rot", "pin", "shape"},
    "set_field": {"ref", "field", "value", "hide", "fields"},
    "move": {"ref", "at", "rot", "mirror", "unit", "by"},
    "delete": {"ref", "label", "pin", "kind", "wire_at", "dangling"},
    "rename_net": {"from", "to"},
    "disconnect": {"pin", "pins"},
    "swap": {"ref", "lib_id", "footprint", "lcsc", "part", "provider"},
    "text": {"text", "at", "size"},
    "pwr_flag": {"net", "at"},
}

_POWER_NAMES: set[str] | None = None


def power_names(project_dir=None) -> set[str]:
    global _POWER_NAMES
    if _POWER_NAMES is None:
        names = set()
        try:
            for lname, e, scope in libs.symbol_index(project_dir):
                if lname == "power" and e.get("pw"):
                    names.add(e["n"])
        except ToolError:
            pass
        names.discard("PWR_FLAG")
        _POWER_NAMES = names
    return _POWER_NAMES


def _near(a, b) -> bool:
    return abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS


def _on_segment(p, a, b) -> bool:
    """p strictly inside segment ab (axis-aligned or not)."""
    if _near(p, a) or _near(p, b):
        return False
    (x0, y0), (x1, y1) = a, b
    cross = (x1 - x0) * (p[1] - y0) - (y1 - y0) * (p[0] - x0)
    if abs(cross) > 1e-3:
        return False
    return min(x0, x1) - EPS <= p[0] <= max(x0, x1) + EPS and min(y0, y1) - EPS <= p[1] <= max(y0, y1) + EPS


class Ctx:
    def __init__(self, pr, sch: Schematic):
        self.pr = pr
        self.sch = sch
        self.changed: list[str] = []
        self.warnings: list[str] = []
        self.refs: dict[str, str] = {}       # op id / temp key -> reference
        self.alias: dict[str, str] = {}      # id -> ref (for pin tokens)
        self.expect: list[dict] = []         # connectivity expectations to verify after save
        self.region: list = []               # changed boxes for preview cropping
        self.pnames = power_names(pr.dir if pr else None)
        self.unnamed: set = set()            # nets KiCad named automatically (tidy keeps them label-free when wired)
        self.inline: dict = {}               # (ref, pin) -> {'net','a','b','done'} wired chain links from layout
        self.power_src: dict = {}            # net -> (lib_id, lib_symbols key) of the power symbol to reuse (tidy)
        self.renamed: set = set()            # net names produced by rename_net (they carry their own warnings)
        self.joins: list = []                # explicit joins (wire points, label at) the net-merge check accepts

    def reserved_boxes(self) -> list:
        return []

    def touch(self, *pts):
        for p in pts:
            self.region.append([p[0], p[1], p[0], p[1]])

    # ── pins ──────────────────────────────────────────────────────────────
    def split_pin(self, token: str) -> tuple[str, str]:
        t = str(token).strip()
        if "." not in t:
            raise ToolError(f"pin reference '{t}' must be REF.PIN (e.g. 'U1.PA9', 'R1.2', 'J1.VBUS')",
                            code="BAD_PIN", fix="use REF.PIN with a pin number or pin name")
        ref, pin = t.split(".", 1)
        ref = self.alias.get(ref, ref)
        return ref, pin

    def resolve(self, token: str, allow_multi=True) -> tuple[list[dict], SymInst | None]:
        """→ groups: [{'si','xy','out','pins':[...],'alternate'}], one per distinct tip location."""
        ref, pin = self.split_pin(token)
        insts = self.sch.find_ref(ref)
        if not insts:
            refs = sorted(s.ref for s in self.sch.symbols(include_power=False))
            where = ref_elsewhere(self.pr, self.sch, ref)
            raise ToolError(f"no symbol '{ref}' on this sheet (pin '{token}')"
                            + (f"; {ref} is on sheet '{where}'" if where else ""), code="REF_NOT_FOUND",
                            fix=(f"pass sheet='{where}' (one sch_apply call edits one sheet)" if where else
                                 f"use a reference ({', '.join(refs[:30])}); temporary ids only live inside the batch "
                                 f"that placed them — earlier batches report id→ref in 'refs'"),
                            data={"refs": refs[:60]})
        lib_pins = insts[0].lib_pins()
        hits, how = G.match_pins(lib_pins, pin)
        if not hits:
            raise ToolError(f"{ref} has no pin '{pin}'" + (f" (from '{token}')" if not str(token).startswith(ref + '.') else ""), code="PIN_NOT_FOUND",
                            fix=f"use a pin number or name of {ref} ({insts[0].lib_id}); close matches: "
                                + ", ".join(G.suggest_pins(lib_pins, pin)) + f" — or sch_read(query='pins', ref='{ref}')",
                            data={"pins": G.suggest_pins(lib_pins, pin, 60)})
        if how == "ambiguous":
            opts = sorted({f"{ref}.{h['number']}({h['name']})" for h in hits})
            raise ToolError(f"'{token}' is ambiguous on {ref}: it can be any of {', '.join(opts)}", code="AMBIGUOUS_PIN",
                            fix="use the pin number or full pin name, e.g. " + opts[0].split("(")[0],
                            data={"candidates": opts})
        groups: list[dict] = []
        missing_units = set()
        for h in hits:
            si = None
            for s in insts:
                if h["unit"] in (0, s.unit) and h["body_style"] in (0, s.body_style):
                    si = s
                    break
            if si is None:
                missing_units.add(h["unit"])
                continue
            m = si.matrix
            xy = G.pin_tip(si.at, m, h)
            out = G.pin_outward(m, h)
            for g in groups:
                if _near(g["xy"], xy):
                    g["pins"].append(h)
                    break
            else:
                groups.append({"si": si, "xy": xy, "out": out, "pins": [h], "alternate": h.get("alternate"),
                               "label": f"{ref}.{h['number']}" + (f"({h['name']})" if h["name"] and h["name"] != h["number"] else "")})
        if not groups:
            u0 = sorted(missing_units)[0]
            raise ToolError(f"pin '{pin}' of {ref} is on unit(s) {sorted(missing_units)} which are not placed",
                            code="UNIT_NOT_PLACED",
                            fix=f'add the unit in the same batch, before this op: {{"op":"place","lib_id":"{insts[0].lib_id}",'
                                f'"ref":"{ref}","unit":{u0}}}')
        if not allow_multi and len(groups) > 1:
            raise ToolError(f"'{token}' matches {len(groups)} pins ({', '.join(g['label'] for g in groups)}) — be specific",
                            code="AMBIGUOUS_PIN", fix="use the pin number, e.g. " + groups[0]["label"].split("(")[0])
        return groups, insts[0]

    # ── connectivity at a point ───────────────────────────────────────────
    def items_at(self, p, exclude_sym: SymInst | None = None, exclude_pin_xy=None) -> dict:
        res = {"wires_end": [], "wires_mid": [], "labels": [], "power": [], "pins": [], "nc": []}
        for w in self.sch.wires():
            if _near(w["a"], p) or _near(w["b"], p):
                res["wires_end"].append(w)
            elif _on_segment(p, w["a"], w["b"]):
                res["wires_mid"].append(w)
        for l in self.sch.labels():
            if _near(l["at"], p):
                res["labels"].append(l)
        for n in self.sch.items("no_connect"):
            a = sexpr.find(n, "at")
            if _near((float(a[1]), float(a[2])), p):
                res["nc"].append(n)
        for s in self.sch.symbols():
            if exclude_sym is not None and s.node is exclude_sym.node:
                continue
            for q in s.pins_world():
                if _near(q["xy"], p):
                    (res["power"] if s.is_power else res["pins"]).append((s, q))
        return res

    def stub_at(self, tip) -> dict | None:
        """Copper-style stub at a pin tip: one wire from tip to Q where Q carries labels / power symbols only."""
        for w in self.sch.wires():
            if _near(w["a"], tip):
                q = w["b"]
            elif _near(w["b"], tip):
                q = w["a"]
            else:
                continue
            at_q = self.items_at(q)
            others = [x for x in at_q["wires_end"] if x["node"] is not w["node"]]
            if others or at_q["pins"] or at_q["wires_mid"]:
                continue
            if at_q["labels"] or at_q["power"]:
                return {"wire": w, "q": q, "labels": at_q["labels"], "power": at_q["power"]}
        return None

    def net_at_pin(self, tip, owner: SymInst | None = None) -> tuple[str | None, str | None]:
        """(net name, how) for a pin tip using local geometry (labels / power symbols / stubs).
        ``owner`` = the symbol the pin belongs to (its own pins at the tip are not 'connections')."""
        st = self.stub_at(tip)
        it = self.items_at(tip, exclude_sym=owner)
        for l in it["labels"]:
            return l["net"], l["kind"]
        for s, q in it["power"]:
            return s.value, "power"
        if st:
            for l in st["labels"]:
                return l["net"], l["kind"]
            for s, q in st["power"]:
                return s.value, "power"
        if it["nc"]:
            return None, "no_connect"
        if it["wires_end"] or it["pins"]:
            # follow the wire run: a label / power symbol anywhere on it names the net
            net = self._net_via_wires(tip)
            if net:
                return net, "wired"
            return None, "wired"
        return None, None

    def _net_via_wires(self, tip):
        wires = self.sch.wires()
        used = [False] * len(wires)
        todo = [tip]
        pts = []
        while todo:
            p = todo.pop()
            pts.append(p)
            for i, w in enumerate(wires):
                if used[i]:
                    continue
                if _near(w["a"], p) or _near(w["b"], p) or _on_segment(p, w["a"], w["b"]):
                    used[i] = True
                    todo += [w["a"], w["b"]]
                    for x in wires:
                        for e in (x["a"], x["b"]):
                            if _on_segment(e, w["a"], w["b"]):
                                todo.append(e)
        for w, u in zip(wires, used):
            if not u:
                continue
            for l in self.sch.labels():
                if _near(l["at"], w["a"]) or _near(l["at"], w["b"]) or _on_segment(l["at"], w["a"], w["b"]):
                    return l["net"]
            for s in self.sch.symbols():
                if s.is_power and not s.lib_id.endswith("PWR_FLAG"):
                    for q in s.pins_world():
                        if _near(q["xy"], w["a"]) or _near(q["xy"], w["b"]):
                            return s.value
        return None

    def remove_attachments(self, tip, keep_nc=False) -> list[str]:
        """Remove what Copper attaches at a pin (stub + label/power, direct label, no_connect)."""
        removed = []
        st = self.stub_at(tip)
        if st:
            self.sch.remove(st["wire"]["node"])
            for l in st["labels"]:
                self.sch.remove(l["node"])
                removed.append(f"label {l['net']}")
            for s, q in st["power"]:
                self.sch.remove(s.node)
                removed.append(f"power {s.value}")
            removed.append("stub wire")
        it = self.items_at(tip)
        for l in it["labels"]:
            self.sch.remove(l["node"])
            removed.append(f"label {l['net']}")
        for s, q in it["power"]:
            self.sch.remove(s.node)
            removed.append(f"power {s.value}")
        if not keep_nc:
            for n in it["nc"]:
                self.sch.remove(n)
                removed.append("no_connect")
        return removed

    def point_is_free(self, q, seg_from=None, owner: SymInst | None = None) -> bool:
        """Nothing connectable at q, and the segment seg_from→q doesn't run over foreign pins/wire ends."""
        it = self.items_at(q)
        if any(it[k] for k in ("wires_end", "wires_mid", "labels", "power", "pins", "nc")):
            return False
        # a stub must not cross any symbol body (including its own)
        if seg_from is not None:
            for s in self.sch.symbols():
                for p in s.pins_world():
                    if _on_segment(p["xy"], seg_from, q):
                        return False
                if not s.is_power:
                    bb = s.body_bbox()
                    if owner is not None and s.node is owner.node:
                        continue
                    if bb[0] + 0.1 < q[0] < bb[2] - 0.1 and bb[1] + 0.1 < q[1] < bb[3] - 0.1:
                        return False
            for w in self.sch.wires():
                if _on_segment(w["a"], seg_from, q) or _on_segment(w["b"], seg_from, q):
                    return False
            for l in self.sch.labels():
                if _on_segment(l["at"], seg_from, q):
                    return False
        return True


# ── helpers ───────────────────────────────────────────────────────────────
def _pins_of(op) -> list[str]:
    if op.get("pins") is not None:
        v = op["pins"]
        if isinstance(v, str):
            v = [x for x in re.split(r"[\s,]+", v) if x]
        return list(v)
    if op.get("pin") is not None:
        return [op["pin"]]
    raise ToolError(f"{op['op']} needs 'pin' or 'pins'", code="BAD_OP", fix='e.g. {"op":"%s","pin":"U1.5"}' % op["op"])


def _xy(v, what="at") -> list[float]:
    if isinstance(v, dict):
        v = [v.get("x"), v.get("y")]
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ToolError(f"'{what}' must be [x, y] in mm", code="BAD_OP")
    try:
        return G.snap_pt([float(v[0]), float(v[1])])
    except (TypeError, ValueError):
        raise ToolError(f"'{what}' must be numbers, got {v!r}", code="BAD_OP")


def _rot(v) -> int:
    if v is None:
        return 0
    try:
        r = int(float(v)) % 360
    except (TypeError, ValueError):
        raise ToolError(f"rot must be 0/90/180/270, got {v!r}", code="BAD_OP")
    if r not in (0, 90, 180, 270):
        raise ToolError(f"rot must be 0/90/180/270, got {v!r}", code="BAD_OP")
    return r


def _mirror(v):
    if v in (None, "", "none", False):
        return None
    if str(v).lower() in ("x", "y"):
        return str(v).lower()
    raise ToolError(f"mirror must be 'x' or 'y', got {v!r}", code="BAD_OP")


def _fmt(p):
    return f"({num(p[0])}, {num(p[1])})"


def _existing_power(ctx: Ctx, net: str) -> tuple[str, str] | None:
    """(lib_id, lib_symbols key) of a power symbol this sheet already uses for ``net`` (stock or a
    project/custom power library such as 'MyLib:VIN')."""
    if net in ctx.power_src:
        return ctx.power_src[net]
    for s in ctx.sch.symbols():
        if s.value != net or s.lib_id.endswith("PWR_FLAG"):
            continue
        ls = ctx.sch.lib_symbol(s.lib_name)
        if ls is not None and sexpr.find(ls, "power") is not None:
            return s.lib_id, s.lib_name
    return None


def _is_power_net(ctx: Ctx, net: str) -> bool:
    return net in ctx.pnames or _existing_power(ctx, net) is not None


def _power_libsym(ctx: Ctx, net: str):
    """Power symbol for a net → (LibSymbol, value, lib_symbols key).

    Re-uses the power symbol the sheet already uses for that net (keeps custom/project power libs such as
    'MyLib:VIN' instead of inventing 'power:VIN'), else an exact stock power-lib match, else a generic
    VCC/GND symbol renamed by Value (KiCad 8+ semantics)."""
    ex = _existing_power(ctx, net)
    tree = ctx.sch.lib_symbol(ex[1]) if ex else None
    if tree is not None:
        lib, _, name = ex[0].partition(":")
        return libs.LibSymbol(ex[0], lib, name, copy.deepcopy(tree), ""), net, ex[1]
    if net in ctx.pnames:
        return libs.read_symbol(f"power:{net}", ctx.pr.dir if ctx.pr else None), None, None
    base = "power:GND" if LAY.is_ground(net) else "power:VCC"
    return libs.read_symbol(base, ctx.pr.dir if ctx.pr else None), net, None


def _norm_net(ctx: Ctx, net) -> str:
    """Accept KiCad's sheet-path form of a net name ('/VBAT', '/power/VBAT' from ERC / netlists) and use the label
    text ('VBAT'): a label literally named '/VBAT' would be a brand-new, unconnected net."""
    net = str(net or "").strip()
    if net.startswith("/") and len(net) > 1 and not net.endswith("/"):
        base = net.rstrip("/").split("/")[-1]
        ctx.warnings.append(f"net '{net}' is KiCad's sheet-path form; used the label name '{base}'")
        return base
    return net


def _check_net_name(ctx: Ctx, net: str):
    """Refuse a net name that differs from an existing one only by letter case ('gnd' vs 'GND'):
    KiCad treats them as two separate nets, which silently splits the net."""
    if net in ctx.pnames:
        return
    known = {l["net"] for l in ctx.sch.labels()} | {s.value for s in ctx.sch.symbols() if s.is_power}
    if net in known:
        return
    low = net.lower()
    hit = next((n for n in sorted(known) + sorted(ctx.pnames) if n.lower() == low), None)
    if hit:
        raise ToolError(f"net '{net}' differs only in letter case from the existing net '{hit}' — KiCad would treat "
                        f"them as two different nets", code="NET_NAME_CASE", fix=f"use net '{hit}'")


def _natural_dir(ls) -> tuple[float, float]:
    """Direction (lib coords) from the pin to the graphic of a power symbol."""
    bb = libs.unit_bbox(ls.tree, 1, 1, include_pins=False)
    pin = ls.pins[0]["at"] if ls.pins else [0, 0]
    cx, cy = (bb[0] + bb[2]) / 2 - pin[0], (bb[1] + bb[3]) / 2 - pin[1]
    if abs(cx) > abs(cy):
        return (1 if cx > 0 else -1, 0)
    return (0, 1 if cy > 0 else -1)


# ── op implementations ───────────────────────────────────────────────────
def _place_power_symbol(ctx: Ctx, net: str, q, out, force_rot: int | None = None) -> SymInst:
    ls, value, key = _power_libsym(ctx, net)
    if key is None:
        key = ctx.sch.embed(ls)
    nat = _natural_dir(ls)
    if force_rot is not None:
        rot = force_rot
    else:
        # graphic points away from the pin along the stub (GND down on bottom pins, rails up on top pins)
        rot = G.rotation_for_direction(nat, out)
    ref = ctx.sch.next_ref("#PWR")
    si = ctx.sch.add_symbol(key, ls.lib_id, ref, q, rot, None, 1, value=value, lib_name=key)
    # the power pin may not be at the symbol origin
    tip = si.pins_world()[0]["xy"] if si.pins_world() else q
    if not _near(tip, q):
        a = sexpr.find(si.node, "at")
        a[1], a[2] = num(q[0] + (q[0] - tip[0])), num(q[1] + (q[1] - tip[1]))
        autoplace_fields(si)
    return si


def _attach(ctx: Ctx, g: dict, net: str, style: str, kind: str = "local", shape: str = "bidirectional") -> str:
    """Stub + label / power symbol at one pin-tip group."""
    tip, out, si = g["xy"], g["out"], g["si"]
    ent = next((ctx.inline.get((si.ref, p["number"])) for p in g["pins"] if ctx.inline.get((si.ref, p["number"]))), None)
    if ent is not None and ent["net"] == net and style == "label":
        r = _inline_wire(ctx, ent, kind)
        if r:
            return r
    cur, how = ctx.net_at_pin(tip, si)
    if cur is not None:
        if cur == net:
            return f"{g['label']} already on {net}"
        raise ToolError(f"{g['label']} is already connected to '{cur}' — refusing to short it to '{net}'",
                        code="NET_CONFLICT", fix=f'{{"op":"disconnect","pin":"{g["label"].split("(")[0]}"}} first, then connect')
    if how == "no_connect":
        ctx.remove_attachments(tip)
        ctx.warnings.append(f"removed no_connect flag on {g['label']} (now connected to {net})")
    elif how == "wired" and style == "label":
        r = _label_on_wire(ctx, g, net, kind, shape)
        if r:
            return r
    # pick a stub length that lands on a free point: straight out first, then sideways (a wire already leaving the
    # pin, or one passing just outside it, takes the straight spots)
    q = None
    body = si.body_bbox()
    for d in (out, (out[1], out[0]), (-out[1], -out[0])):
        for k in ((1, 2, 3, 4, 6) if d is out else (1, 2)):
            cand = [round(tip[0] + d[0] * STUB * k, 4), round(tip[1] + d[1] * STUB * k, 4)]
            if d is not out and _seg_hits_box(tip, cand, body):
                continue
            if ctx.point_is_free(cand, seg_from=tip, owner=si):
                q = cand
                break
        if q is not None:
            out = d
            break
    if q is None:
        raise ToolError(f"no free spot to attach a label to {g['label']} (something sits right outside the pin)",
                        code="NO_ROOM", fix=f"move the part blocking {g['label']} or use sch_tidy")
    if how == "wired":
        ctx.warnings.append(f"{g['label']} already has a wire; the {'power symbol' if style == 'power' else 'label'} "
                            f"was added on a separate stub")
    ctx.sch.add_wire(tip, q)
    if g.get("alternate"):
        _set_alternate(si, g["pins"][0]["number"], g["alternate"])
    ctx.touch(tip, q)
    if style == "power":
        ps = _place_power_symbol(ctx, net, q, out)
        ctx.expect.append({"pins": [(si.ref, p["number"]) for p in g["pins"]], "net": net})
        return f"{g['label']} → {net} (power symbol {ps.ref})"
    ctx.sch.add_label(net, q, G.label_angle(out), kind, shape)
    ctx.expect.append({"pins": [(si.ref, p["number"]) for p in g["pins"]], "net": net, "local": kind == "local"})
    return f"{g['label']} → {net} ({kind} label)"


def _label_on_wire(ctx: Ctx, g: dict, net: str, kind: str, shape: str) -> str | None:
    """Name a pin's existing (unnamed) wire: put the label on the wire itself, on a grid point near the pin where
    nothing else sits and the text clears symbol bodies. None if no such point exists."""
    tip, si = g["xy"], g["si"]
    wires = ctx.sch.wires()
    run, _ = _run_of(wires, tip, _dots(ctx))
    busy = {_key(l["at"]) for l in ctx.sch.labels()}
    busy |= {_key(q["xy"]) for s in ctx.sch.symbols() for q in s.pins_world()}
    for n in ctx.sch.items("junction") + ctx.sch.items("no_connect"):
        a = sexpr.find(n, "at")
        busy.add(_key((float(a[1]), float(a[2]))))
    bodies = [s.body_bbox() for s in ctx.sch.symbols() if not s.is_power]
    lboxes = [LAY.label_box(l, pad=0.3) for l in ctx.sch.labels()]
    foreign = _Blocked(ctx, [tip])                         # a label on a crossing wire would join that net too
    cands = []
    for i in sorted(run):
        w = wires[i]
        a, b = (w["a"], w["b"]) if _near(w["a"], tip) or not _near(w["b"], tip) else (w["b"], w["a"])
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = max(abs(dx), abs(dy))
        if n < EPS or (abs(dx) > EPS and abs(dy) > EPS):
            continue                                       # diagonal wires: no
        ux, uy = dx / n, dy / n
        steps = int(n / 1.27 + EPS)
        for j in range(1, steps + 1):
            p = [round(a[0] + ux * 1.27 * j, 4), round(a[1] + uy * 1.27 * j, 4)]
            d = abs(p[0] - tip[0]) + abs(p[1] - tip[1])
            cands.append((0 if _near(a, tip) else 1, abs(d - STUB), d, p, (ux, uy)))
    for _, _, _, p, (ux, uy) in sorted(cands, key=lambda c: c[:3]):
        if _key(p) in busy or foreign.on_wire(p):
            continue
        for ang in ((0, 180) if abs(uy) < EPS else (90, 270)):
            lb = LAY.label_box({"at": p, "net": net, "kind": kind, "rot": ang})
            if any(LAY._rect_hit(lb, bb, -0.1) for bb in bodies) or any(LAY._rect_hit(lb, ob) for ob in lboxes):
                continue
            ctx.sch.add_label(net, p, ang, kind, shape)
            ctx.touch(p)
            ctx.expect.append({"pins": [(si.ref, q["number"]) for q in g["pins"]], "net": net, "local": kind == "local"})
            return f"{g['label']} → {net} ({kind} label on its wire)"
    return None


def _inline_wire(ctx: Ctx, ent: dict, kind: str) -> str | None:
    """Stacked chain link from auto-layout: a short vertical wire between the two pins + a label on it."""
    if ent["done"]:
        return f"{ent['a'][0]}.{ent['a'][1]} — {ent['b'][0]}.{ent['b'][1]} already wired ({ent['net']})"
    tips = []
    for ref, pin in (ent["a"], ent["b"]):
        insts = ctx.sch.find_ref(ref)
        hit = None
        for si in insts:
            for p in si.pins_world():
                if p["number"] == pin:
                    hit = (si, p)
        if hit is None:
            return None
        tips.append(hit)
    (sa, pa), (sb, pb) = tips
    a, b = pa["xy"], pb["xy"]
    if abs(a[0] - b[0]) > EPS or ctx.net_at_pin(a, sa)[1] or ctx.net_at_pin(b, sb)[1]:
        return None   # geometry changed or pins already used → fall back to labels
    ctx.sch.add_wire(a, b)
    ent["done"] = True
    ctx.touch(a, b)
    if ent["net"] in ctx.unnamed:
        ctx.expect.append({"same": [(sa.ref, pa["number"]), (sb.ref, pb["number"])]})
        return f"{sa.ref}.{pa['number']} — {sb.ref}.{pb['number']} wired"
    mid = [a[0], G.snap((a[1] + b[1]) / 2)]
    ctx.sch.add_label(ent["net"], mid, 180, kind)
    ctx.expect.append({"pins": [(sa.ref, pa["number"]), (sb.ref, pb["number"])], "net": ent["net"], "local": kind == "local"})
    return f"{sa.ref}.{pa['number']} — {sb.ref}.{pb['number']} wired as {ent['net']}"


def _set_alternate(si: SymInst, number: str, alt: str):
    for p in sexpr.find_all(si.node, "pin"):
        if str(p[1]) == number:
            for e in list(p):
                if isinstance(e, list) and e and e[0] == "alternate":
                    p.remove(e)
            p.append([Sym("alternate"), alt])


def op_connect(ctx: Ctx, op, force_power=False):
    net = _norm_net(ctx, op.get("net"))
    if not net:
        raise ToolError("connect needs 'net'", code="BAD_OP", fix='{"op":"connect","pin":"U1.PA9","net":"USB_DP"}')
    if re.search(r"[\s]", net):
        raise ToolError(f"net name '{net}' contains whitespace", code="BAD_OP", fix="use e.g. USB_DP or I2C_SDA")
    kind = (op.get("kind") or "local").lower()
    style = (op.get("style") or "").lower()
    if force_power or style == "power":
        style = "power"
    elif style in ("label", "global", "local", "hierarchical"):
        if style != "label":
            kind = style
        style = "label"
    else:
        style = "power" if _is_power_net(ctx, net) and kind == "local" else "label"
    if kind not in ("local", "global", "hierarchical"):
        raise ToolError(f"kind must be local/global/hierarchical, got {kind}", code="BAD_OP")
    _check_net_name(ctx, net)
    # a net already used ONLY as a global label on this sheet keeps that kind (a local label of the same name
    # would be a different net). A hierarchical label joins local labels of its name on the sheet, so joining an
    # existing hierarchical net uses a plain local label (no new sheet-interface label).
    if style == "label" and kind == "local" and not op.get("kind"):
        here = {l["kind"] for l in ctx.sch.labels() if l["net"] == net}
        if "global" in here and "local" not in here:
            kind = "global"
    msgs = []
    all_groups = []
    for tok in _pins_of(op):
        groups, _ = ctx.resolve(tok)
        all_groups += groups
    if style == "power":
        all_groups, rail_msgs = _power_rails(ctx, all_groups, net)
        msgs += rail_msgs
    for g in all_groups:
        msgs.append(_attach(ctx, g, net, style, kind))
    ctx.changed += msgs


RAIL_PITCH = 7.62   # same-net pins closer than this on a top/bottom edge share one rail + power symbol


def _power_rails(ctx: Ctx, groups: list[dict], net: str) -> tuple[list[dict], list[str]]:
    """Adjacent pins on one edge of one symbol going to the same power net: short stubs onto one rail wire with a
    single power symbol (avoids a row of overlapping '+3V3+3V3…' / 'GNDGND' symbols). Works on every edge."""
    rest, msgs = [], []
    buckets: dict = {}
    for g in groups:
        buckets.setdefault((id(g["si"].node), tuple(g["out"])), []).append(g)
    for (_, out), gs in buckets.items():
        along = 0 if out[0] == 0 else 1          # coordinate that varies along the edge
        gs.sort(key=lambda g: g["xy"][along])
        # every other pin tip on that edge: a rail must never run past a pin of another net (it would short it)
        mine = {(round(g["xy"][0], 3), round(g["xy"][1], 3)) for g in gs}
        others = [p["xy"][along] for p in gs[0]["si"].pins_world()
                  if tuple(p["out"]) == out and abs(p["xy"][1 - along] - gs[0]["xy"][1 - along]) < EPS
                  and (round(p["xy"][0], 3), round(p["xy"][1], 3)) not in mine]
        runs, cur = [], [gs[0]]
        for g in gs[1:]:
            lo, hi = cur[-1]["xy"][along], g["xy"][along]
            if hi - lo <= RAIL_PITCH + EPS and not any(lo - EPS < x < hi + EPS for x in others):
                cur.append(g)
            else:
                runs.append(cur)
                cur = [g]
        runs.append(cur)
        for run in runs:
            free = [g for g in run if ctx.net_at_pin(g["xy"], g["si"])[1] is None]
            if len(free) < 2 or len(free) != len(run):
                rest += run
                continue
            if len({round(g["xy"][1 - along], 3) for g in run}) != 1:
                rest += run
                continue
            qs = [[g["xy"][0] + out[0] * STUB, g["xy"][1] + out[1] * STUB] for g in run]
            if not all(ctx.point_is_free(q, seg_from=g["xy"], owner=g["si"]) for g, q in zip(run, qs)):
                rest += run
                continue
            on_rail = [w for w in ctx.sch.wires() for e in (w["a"], w["b"]) if _on_segment(e, qs[0], qs[-1])]
            on_rail += [l for l in ctx.sch.labels() if _on_segment(l["at"], qs[0], qs[-1])]
            if on_rail:
                rest += run
                continue
            for g, q in zip(run, qs):
                ctx.sch.add_wire(g["xy"], q)
                ctx.touch(g["xy"], q)
            ctx.sch.add_wire(qs[0], qs[-1])
            for q in qs[1:-1]:
                ctx.sch.add_junction(q)
            # one power symbol in the middle of the rail
            mid = qs[len(qs) // 2] if len(qs) % 2 else qs[len(qs) // 2 - 1]
            ext = [mid[0] + out[0] * STUB, mid[1] + out[1] * STUB]
            ctx.sch.add_wire(mid, ext)
            if mid is qs[0] or mid is qs[-1]:
                ctx.sch.add_junction(mid)      # stub + rail end + riser meet here
            ps = _place_power_symbol(ctx, net, ext, out)
            si = run[0]["si"]
            ctx.expect.append({"pins": [(si.ref, p["number"]) for g in run for p in g["pins"]], "net": net})
            msgs.append(f"{', '.join(g['label'] for g in run)} → {net} (shared rail, power symbol {ps.ref})")
    return rest, msgs


def op_no_connect(ctx: Ctx, op):
    for tok in _pins_of(op):
        groups, si0 = ctx.resolve(tok)
        if si0 is not None and si0.is_power:
            raise ToolError(f"{si0.ref} is a {'PWR_FLAG' if is_flag(si0) else 'power symbol'} — a no-connect flag on it is "
                            f"meaningless", code="BAD_OP",
                            fix=f'remove it instead: {{"op":"delete","ref":"{si0.ref}"}} (its wire goes with it)')
        for g in groups:
            cur, how = ctx.net_at_pin(g["xy"], g["si"])
            if how == "no_connect":
                ctx.changed.append(f"{g['label']} already no-connect")
                continue
            if cur is not None or how == "wired":
                raise ToolError(f"{g['label']} is connected ({cur or 'wired'}); a no_connect flag would be wrong",
                                code="NET_CONFLICT", fix=f'{{"op":"disconnect","pin":"{g["label"].split("(")[0]}"}} first')
            ctx.sch.add_no_connect(g["xy"])
            ctx.touch(g["xy"])
            ctx.changed.append(f"{g['label']} no-connect")


def _route_l(a, out_a, b):
    if abs(a[0] - b[0]) < EPS or abs(a[1] - b[1]) < EPS:
        return [a, b]
    corner = [b[0], a[1]] if out_a[0] != 0 else [a[0], b[1]]
    return [a, corner, b]


def _wired_together(ctx: "Ctx", A, B) -> bool:
    """True if a run of wires already joins points A and B (T-joins count only at a junction, as in KiCad)."""
    if _near(A, B):
        return True
    return _key(B) in _run_of(ctx.sch.wires(), A, _dots(ctx))[1]


def _seg_hits_box(p, q, bb, shrink=0.2) -> bool:
    x0, y0, x1, y1 = bb[0] + shrink, bb[1] + shrink, bb[2] - shrink, bb[3] - shrink
    if x0 >= x1 or y0 >= y1:
        return False
    if abs(p[1] - q[1]) < EPS:   # horizontal
        y = p[1]
        return y0 < y < y1 and min(p[0], q[0]) < x1 and max(p[0], q[0]) > x0
    if abs(p[0] - q[0]) < EPS:   # vertical
        x = p[0]
        return x0 < x < x1 and min(p[1], q[1]) < y1 and max(p[1], q[1]) > y0
    return True


def _key(p) -> tuple:
    return (round(p[0] * 100), round(p[1] * 100))


def _dots(ctx: "Ctx") -> set:
    return {_key((float(a[1]), float(a[2]))) for a in (sexpr.find(n, "at") for n in ctx.sch.items("junction"))}


def _run_of(wires: list, start, dots: set | None = None) -> tuple[set, set]:
    """The wire run (with T-joins) that touches ``start``: (indexes into ``wires``, point keys on it).
    With ``dots`` (junction keys), a point in the middle of a wire joins it only where a junction sits, as in KiCad."""
    used, pts, todo = set(), {_key(start)}, [start]

    def mid_ok(p):
        return dots is None or _key(p) in dots

    while todo:
        p = todo.pop()
        for i, w in enumerate(wires):
            if i in used or not (_near(w["a"], p) or _near(w["b"], p) or (_on_segment(p, w["a"], w["b"]) and mid_ok(p))):
                continue
            used.add(i)
            nxt = [w["a"], w["b"]] + [e for x in wires for e in (x["a"], x["b"])
                                      if _on_segment(e, w["a"], w["b"]) and mid_ok(e)]
            for e in nxt:
                if _key(e) not in pts:
                    pts.add(_key(e))
                    todo.append(e)
    return used, pts


class _PtIndex:
    """Point keys indexed by row and column, for fast 'which points lie on this segment' queries."""

    def __init__(self, keys):
        self.keys = set(keys)
        self.by_y: dict = {}
        self.by_x: dict = {}
        for k in self.keys:
            self.by_y.setdefault(k[1], []).append(k[0])
            self.by_x.setdefault(k[0], []).append(k[1])

    def on(self, p, q) -> list:
        """Keys on segment p→q, both ends included."""
        if abs(p[1] - q[1]) < EPS:
            lo, hi = round(min(p[0], q[0]) * 100), round(max(p[0], q[0]) * 100)
            return [(x, round(p[1] * 100)) for x in self.by_y.get(round(p[1] * 100), ()) if lo <= x <= hi]
        if abs(p[0] - q[0]) < EPS:
            lo, hi = round(min(p[1], q[1]) * 100), round(max(p[1], q[1]) * 100)
            return [(round(p[0] * 100), y) for y in self.by_x.get(round(p[0] * 100), ()) if lo <= y <= hi]
        return [k for k in self.keys if _near((k[0] / 100, k[1] / 100), q) or _on_segment((k[0] / 100, k[1] / 100), p, q)
                or _near((k[0] / 100, k[1] / 100), p)]


class _Blocked:
    """What a new wire must not touch: every connectable item (wire end, label anchor, junction, no-connect, pin tip,
    power symbol pin) that is not on the wire run(s) of the pins being joined. A wire through such a point, or a wire
    corner on a foreign wire, would join that net (KiCad connects a label anywhere on a wire, and Copper adds a
    junction where wires T)."""

    def __init__(self, ctx: "Ctx", own_starts):
        sch = ctx.sch
        wires = sch.wires()
        own_w, own = set(), set()
        dots = _dots(ctx)
        for s in own_starts:
            w, p = _run_of(wires, s, dots)
            own_w |= w
            own |= p
        self.ctx = ctx
        self.pts: dict = {}          # key -> (kind, name, xy)
        for i, w in enumerate(wires):
            if i not in own_w:
                for e in (w["a"], w["b"]):
                    self.pts.setdefault(_key(e), ("wire", None, e))
        for kind in ("junction", "no_connect"):
            for n in sch.items(kind):
                a = sexpr.find(n, "at")
                xy = (float(a[1]), float(a[2]))
                self.pts[_key(xy)] = (kind, None, xy)
        self.soft = set()            # where a free pin's future connect stub would end: avoid if possible
        ends = {_key(e) for w in wires for e in (w["a"], w["b"])}
        for s in sch.symbols():
            for q in s.pins_world():
                k = _key(q["xy"])
                self.pts[k] = ("power", s.value, q["xy"]) if s.is_power else ("pin", f"{s.ref}.{q['number']}", q["xy"])
                if not s.is_power and k not in ends and k not in own:
                    self.soft.add(_key((q["xy"][0] + q["out"][0] * STUB, q["xy"][1] + q["out"][1] * STUB)))
        for l in sch.labels():
            self.pts[_key(l["at"])] = ("label", l["net"], l["at"])
        for k in own:
            self.pts.pop(k, None)
        self.index = _PtIndex(self.pts)
        self.soft = _PtIndex(self.soft)
        self.hw: dict = {}           # foreign horizontal wires by y key: [(x0, x1)]
        self.vw: dict = {}
        self.dw: list = []
        for i, w in enumerate(wires):
            if i in own_w:
                continue
            (x0, y0), (x1, y1) = w["a"], w["b"]
            if abs(y0 - y1) < EPS:
                self.hw.setdefault(round(y0 * 100), []).append((min(x0, x1), max(x0, x1)))
            elif abs(x0 - x1) < EPS:
                self.vw.setdefault(round(x0 * 100), []).append((min(y0, y1), max(y0, y1)))
            else:
                self.dw.append(w)

    def on_wire(self, v) -> bool:
        """v lies on a foreign wire (end or interior)."""
        x, y = v
        if any(a - EPS <= x <= b + EPS for a, b in self.hw.get(round(y * 100), ())):
            return True
        if any(a - EPS <= y <= b + EPS for a, b in self.vw.get(round(x * 100), ())):
            return True
        return any(_near(v, w["a"]) or _near(v, w["b"]) or _on_segment(v, w["a"], w["b"]) for w in self.dw)

    def overlaps(self, p, q) -> bool:
        """Segment p→q runs along a foreign wire."""
        if abs(p[1] - q[1]) < EPS:
            lo, hi = min(p[0], q[0]), max(p[0], q[0])
            return any(min(hi, b) - max(lo, a) > EPS for a, b in self.hw.get(round(p[1] * 100), ()))
        if abs(p[0] - q[0]) < EPS:
            lo, hi = min(p[1], q[1]), max(p[1], q[1])
            return any(min(hi, b) - max(lo, a) > EPS for a, b in self.vw.get(round(p[0] * 100), ()))
        return False

    def conflicts(self, pts: list) -> list:
        """Foreign point keys a polyline would join (empty = clean). pts[0] / pts[-1] are the pins being wired."""
        bad = []
        for p, q in zip(pts, pts[1:]):
            bad += self.index.on(p, q)
            if self.overlaps(p, q):
                bad.append(_key(p))
        for v in pts[1:-1]:
            if self.on_wire(v):
                bad.append(_key(v))
        return bad

    def soft_hits(self, pts: list) -> int:
        return sum(len(self.soft.on(p, q)) for p, q in zip(pts, pts[1:]))

    def describe(self, k) -> str:
        kind, name, xy = self.pts.get(k) or ("wire", None, (k[0] / 100, k[1] / 100))
        net = None
        if kind in ("label", "power"):
            net = name
        elif kind == "pin":
            net = self.ctx.net_at_pin(xy)[0] or self.ctx._net_via_wires(xy)
        else:
            net = self.ctx._net_via_wires(xy)
        what = {"label": f"label {name}", "power": f"power symbol {name}", "pin": f"pin {name}",
                "junction": "a junction", "no_connect": "a no-connect flag"}.get(kind, "a wire end")
        return f"{what} at {_fmt(xy)}" + (f" (net '{net}')" if net and kind not in ("label", "power") else "")


def _clean_path(c: list) -> list:
    pts = [c[0]]
    for p in c[1:]:
        if not _near(p, pts[-1]):
            pts.append(p)
    clean = [pts[0]]
    for i in range(1, len(pts) - 1):                      # drop collinear midpoints
        p0, p1, p2 = clean[-1], pts[i], pts[i + 1]
        if (abs(p0[0] - p1[0]) < EPS and abs(p1[0] - p2[0]) < EPS) or (abs(p0[1] - p1[1]) < EPS and abs(p1[1] - p2[1]) < EPS):
            continue
        clean.append(p1)
    clean.append(pts[-1])
    out = [clean[0]]                                      # a path that doubles back collapses to a repeated point
    for p in clean[1:]:
        if not _near(p, out[-1]):
            out.append(p)
    return out


def _route(ctx: "Ctx", a: dict, b: dict, blocked: "_Blocked | None" = None) -> list:
    """Orthogonal route between two pin tips that leaves each pin outward, avoids symbol bodies and never touches
    another net (see _Blocked). Raises NO_ROUTE when every candidate would join a foreign net."""
    A, B = a["xy"], b["xy"]
    oa, ob = a["out"], b["out"]
    blocked = blocked or _Blocked(ctx, [A, B])
    bodies = []
    for s in ctx.sch.symbols():
        if not s.is_power:
            ls = ctx.sch.lib_symbol(s.lib_name)
            gb = libs.unit_bbox(ls, s.unit, s.body_style, include_pins=False) if ls is not None else None
            if gb:
                bodies.append(G.bbox_world(s.at, s.matrix, gb))
    A2 = [A[0] + oa[0] * STUB, A[1] + oa[1] * STUB]
    B2 = [B[0] + ob[0] * STUB, B[1] + ob[1] * STUB]
    cands = [_route_l(A, oa, B), [A, A2, [A2[0], B2[1]], B2, B], [A, A2, [B2[0], A2[1]], B2, B]]
    if abs(A[0] - B[0]) < EPS or abs(A[1] - B[1]) < EPS:
        cands.insert(0, [A, B])                           # straight line; a diagonal is never offered
    ys = [min(bb[1] for bb in bodies) - STUB, max(bb[3] for bb in bodies) + STUB] if bodies else []
    xs = [min(bb[0] for bb in bodies) - STUB, max(bb[2] for bb in bodies) + STUB] if bodies else []
    near = [bb for bb in bodies if _seg_hits_box(A2, [A2[0], B2[1]], bb) or _seg_hits_box(A2, [B2[0], A2[1]], bb)
            or _seg_hits_box(A, B, bb)]
    texts = a["si"].field_boxes() + (b["si"].field_boxes() if b["si"].node is not a["si"].node else [])
    if near:
        near = near + [t for t in texts if any(LAY._rect_hit(t, bb, 3.0) for bb in near)]
        ys = [min(bb[1] for bb in near) - STUB, max(bb[3] for bb in near) + STUB]
        xs = [min(bb[0] for bb in near) - STUB, max(bb[2] for bb in near) + STUB]
    for y in ys:
        y = G.snap(y)
        cands.append([A, A2, [A2[0], y], [B2[0], y], B2, B])
    for x in xs:
        x = G.snap(x)
        cands.append([A, A2, [x, A2[1]], [x, B2[1]], B2, B])

    def evaluate(cs, best, worst):
        for c in cs:
            clean = _clean_path(c)
            bad = blocked.conflicts(clean)
            if bad:
                if worst is None or len(bad) < len(worst):
                    worst = bad
                continue
            segs = list(zip(clean, clean[1:]))
            diag = sum(1 for p, q in segs if abs(p[0] - q[0]) > EPS and abs(p[1] - q[1]) > EPS)
            hits = sum(1 for p, q in segs for bb in bodies if _seg_hits_box(p, q, bb))
            hits += 0.1 * sum(1 for p, q in segs for bb in texts if _seg_hits_box(p, q, bb, 0.0))
            length = sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in segs)
            cost = (diag * 1000 + hits * 100 + blocked.soft_hits(clean) * 10, len(segs), length)
            if best is None or cost < best[0]:
                best = (cost, clean)
        return best, worst

    best, worst = evaluate(cands, None, None)
    if best is None or best[0][0] >= 10:
        # detours: escape further out of each pin, then run along any nearby grid line
        more = []
        lines_y = sorted({G.snap(c + j * STUB) for c in (A[1], B[1]) for j in range(-6, 7)} | {G.snap(y) for y in ys})
        lines_x = sorted({G.snap(c + j * STUB) for c in (A[0], B[0]) for j in range(-6, 7)} | {G.snap(x) for x in xs})
        for ka in (1, 2, 3, 4):
            EA = [A[0] + oa[0] * STUB * ka, A[1] + oa[1] * STUB * ka]
            for kb in (1, 2, 3, 4):
                EB = [B[0] + ob[0] * STUB * kb, B[1] + ob[1] * STUB * kb]
                more += [[A, EA, [EA[0], EB[1]], EB, B], [A, EA, [EB[0], EA[1]], EB, B]]
                more += [[A, EA, [EA[0], y], [EB[0], y], EB, B] for y in lines_y]
                more += [[A, EA, [x, EA[1]], [x, EB[1]], EB, B] for x in lines_x]
        best, worst = evaluate(more, best, worst)
    if best is None:
        seen, why = set(), []
        for k in worst or []:
            if k not in seen:
                seen.add(k)
                why.append(blocked.describe(k))
        raise ToolError(f"no clean route for a wire {a['label']} → {b['label']}: every path touches another net "
                        f"({'; '.join(why[:3])})", code="NO_ROUTE",
                        fix=f'join them by name instead: {{"op":"connect","pins":["{a["label"].split("(")[0]}",'
                            f'"{b["label"].split("(")[0]}"],"net":"<NAME>"}}, or move a part')
    if best[0][0] >= 100:
        ctx.warnings.append("wire route crosses a symbol body; consider moving parts or using labels")
    return best[1]


def op_wire(ctx: Ctx, op):
    if op.get("points"):
        pts = [_xy(p, "points[]") for p in op["points"]]
        if len(pts) < 2:
            raise ToolError("wire 'points' needs at least 2 points", code="BAD_OP")
    else:
        if not op.get("from") or not op.get("to"):
            raise ToolError("wire needs 'from' and 'to' pin refs (or 'points')", code="BAD_OP",
                            fix='{"op":"wire","from":"R1.2","to":"D1.A"}')
        ga, _ = ctx.resolve(op["from"], allow_multi=False)
        gb, _ = ctx.resolve(op["to"], allow_multi=False)
        a, b = ga[0], gb[0]
        na, _ = ctx.net_at_pin(a["xy"], a["si"])
        nb, _ = ctx.net_at_pin(b["xy"], b["si"])
        if na and nb and na != nb:
            raise ToolError(f"wiring {a['label']} ({na}) to {b['label']} ({nb}) would short two nets",
                            code="NET_CONFLICT", fix="disconnect one pin first")
        if _wired_together(ctx, a["xy"], b["xy"]):
            ctx.changed.append(f"{a['label']} and {b['label']} are already wired together")
            return
        blocked = _Blocked(ctx, [a["xy"], b["xy"]])
        pts = _route(ctx, a, b, blocked)
        pins = [(g["si"].ref, p["number"]) for g in (a, b) for p in g["pins"]]
        # a wire joins whatever is already wired to either end (KiCad leaves NC-type pins out of its netlist, so
        # their wired neighbours would look unrelated to the merge check)
        wires, dots = ctx.sch.wires(), _dots(ctx)
        keys = _run_of(wires, a["xy"], dots)[1] | _run_of(wires, b["xy"], dots)[1]
        ctx.joins.append({"pins": [(s.ref, q["number"]) for s in ctx.sch.symbols(include_power=False)
                                   for q in s.pins_world() if _key(q["xy"]) in keys] + pins})
        if na or nb:
            ctx.expect.append({"pins": pins, "net": na or nb, "local": True})
        else:
            ctx.expect.append({"same": pins})
    if op.get("points"):
        blocked = None
        # explicit geometry: the caller joins whatever sits at the points they gave, nothing else
        j = _touching(ctx, pts)
        ctx.joins.append(j)
        named = sorted(set(j["names"]))
        if len(named) > 1:
            ctx.warnings.append(f"this wire joins the nets {', '.join(named)} into one (ERC will report the extra names)")
    for p, q in zip(pts, pts[1:]):
        if _near(p, q):
            continue
        if abs(p[0] - q[0]) > EPS and abs(p[1] - q[1]) > EPS:
            ctx.warnings.append(f"diagonal wire segment {_fmt(p)}→{_fmt(q)}")
        ctx.sch.add_wire(p, q)
        ctx.touch(p, q)
    ctx.changed.append("wire " + " → ".join(_fmt(p) for p in pts))
    _junctions(ctx, pts, blocked)


def _touching(ctx: Ctx, points) -> dict:
    """Pins and net names reached from these points through the existing wiring: {'pins': [(ref, pin)], 'names': [..]}."""
    wires = ctx.sch.wires()
    dots = _dots(ctx) | {_key(p) for p in points}   # a given point joins the wire it lies on; elsewhere KiCad's rule
    keys: set = set()
    for p in points:
        keys |= _run_of(wires, p, dots)[1]
    pins = [(s.ref, q["number"]) for s in ctx.sch.symbols(include_power=False) for q in s.pins_world() if _key(q["xy"]) in keys]
    names = [l["net"] for l in ctx.sch.labels() if _key(l["at"]) in keys]
    names += [s.value for s in ctx.sch.symbols() if s.is_power and not is_flag(s)
              for q in s.pins_world() if _key(q["xy"]) in keys]
    return {"pins": pins, "names": names}


def _junctions(ctx: Ctx, pts, blocked: "_Blocked | None" = None):
    """Add junctions where new wire points T-into other wires or ≥3 wire ends meet — never on a point that belongs to
    another net than the wire's own (``blocked``), which would join the two."""
    have = [(float(sexpr.find(n, "at")[1]), float(sexpr.find(n, "at")[2])) for n in ctx.sch.items("junction")]
    cands = []
    for p in pts:
        cands.append(p)
    # also points of other wires lying on the new segments
    for w in ctx.sch.wires():
        for e in (w["a"], w["b"]):
            for p, q in zip(pts, pts[1:]):
                if _on_segment(e, p, q):
                    cands.append(e)
    if blocked is not None:
        cands = [p for p in cands if _key(p) not in blocked.index.keys and not blocked.on_wire(p)]
    for p in cands:
        it = ctx.items_at(p)
        n_end = len(it["wires_end"])
        n_mid = len(it["wires_mid"])
        n_pin = len(it["pins"]) + len(it["power"])
        if (n_mid and n_end) or n_end + n_pin >= 3 or (n_mid >= 1 and n_end >= 1):
            if not any(_near(h, p) for h in have):
                ctx.sch.add_junction(p)
                have.append(tuple(p))
                ctx.changed.append(f"junction {_fmt(p)}")


def op_label(ctx: Ctx, op):
    net = _norm_net(ctx, op.get("net"))
    if not net:
        raise ToolError("label needs 'net'", code="BAD_OP")
    kind = (op.get("kind") or "local").lower()
    if op.get("pin"):
        op2 = dict(op, op="connect", style="label", kind=kind)
        return op_connect(ctx, op2)
    if op.get("at") is None:
        raise ToolError("label needs 'at' [x,y] or 'pin'", code="BAD_OP",
                        fix='{"op":"label","net":"SDA","pin":"U1.PB7"} — prefer pin-anchored labels')
    p = _xy(op["at"])
    _check_net_name(ctx, net)
    it = ctx.items_at(p)
    if not any(it[k] for k in ("wires_end", "wires_mid", "pins", "power")):
        ctx.warnings.append(f"label {net} at {_fmt(p)} touches no wire or pin (it will dangle)")
    j = _touching(ctx, [p])
    dots = [sexpr.find(n, "at") for n in ctx.sch.items("junction")]
    if len(it["wires_mid"]) + len(it["wires_end"]) >= 2 and not any(_near((float(a[1]), float(a[2])), p) for a in dots):
        wires = ctx.sch.wires()
        runs = {frozenset(_run_of(wires, w["a"], _dots(ctx))[0]) for w in it["wires_mid"] + it["wires_end"]}
        if len(runs) > 1:
            ctx.warnings.append(f"label {net} at {_fmt(p)} sits on {len(runs)} separate wire runs and joins them")
    ctx.joins.append({"pins": j["pins"], "names": j["names"] + [net]})
    ctx.sch.add_label(net, p, _rot(op.get("rot")), kind, op.get("shape") or "bidirectional")
    ctx.touch(p)
    ctx.changed.append(f"{kind} label {net} at {_fmt(p)}")


def op_text(ctx: Ctx, op):
    if not op.get("text"):
        raise ToolError("text needs 'text'", code="BAD_OP")
    if op.get("at") is None:
        area, _ = LAY.usable(ctx.sch)
        w = max(text_width(t, float(op.get("size") or 1.27)) for t in str(op["text"]).split("\n"))
        box = [0, -2, w, 0.5 + 1.6 * str(op["text"]).count("\n")]
        off = LAY.find_spot(box, area, LAY.occupied(ctx.sch))
        p = G.snap_pt(off) if off else [area[0], area[1] + 2.54]
    else:
        p = _xy(op["at"])
    ctx.sch.add_text(str(op["text"]), p, float(op.get("size") or 1.27))
    ctx.touch(p)
    ctx.changed.append(f"text '{str(op['text'])[:40]}' at {_fmt(p)}")


def ref_elsewhere(pr, sch: Schematic, ref: str) -> str | None:
    """Sheet argument that reaches reference ``ref`` when it is not on the current sheet INSTANCE: another sheet
    file, or — for a sheet used several times — the instance (sheet name) whose annotation holds it."""
    if pr is None or not ref:
        return None
    try:
        hs = hierarchy(pr)
    except Exception:  # noqa: BLE001
        return None
    texts: dict = {}
    want = re.escape(ref)
    for h in hs:
        if sch.path is not None and _same(h["file"], sch.path) and h["path"] == sch.sheet_path:
            continue
        k = str(h["file"]).lower()
        if k not in texts:
            try:
                texts[k] = h["file"].read_text(encoding="utf-8")
            except OSError:
                texts[k] = ""
        text = texts[k]
        hit = re.search(r'\(path "' + re.escape(h["path"]) + r'"\s*\(reference "' + want + '"', text)
        if hit is None and h["parent"] is None:
            hit = re.search(r'\(property "Reference" "' + want + '"', text) if not re.search(r'\(instances', text) else None
        if hit:
            if h["parent"] is None:
                return h["file"].name
            multi = sum(1 for x in hs if _same(x["file"], h["file"])) > 1
            return (h["name"] or h["file"].name) if multi else h["file"].name
    return None


def _instances(ctx: Ctx, ref: str, unit=None) -> list[SymInst]:
    ref = ctx.alias.get(ref, ref)
    insts = ctx.sch.find_ref(ref)
    if not insts:
        where = ref_elsewhere(ctx.pr, ctx.sch, ref)
        raise ToolError(f"no symbol '{ref}' on this sheet" + (f"; it is on sheet '{where}'" if where else ""),
                        code="REF_NOT_FOUND",
                        fix=f"pass sheet='{where}'" if where else "sch_read(query='components') to list references")
    if unit is not None:
        insts = [s for s in insts if s.unit == int(unit)]
        if not insts:
            raise ToolError(f"{ref} unit {unit} is not placed", code="UNIT_NOT_PLACED")
    return insts


def op_set_field(ctx: Ctx, op):
    ref = op.get("ref")
    if not ref:
        raise ToolError("set_field needs 'ref'", code="BAD_OP")
    fields = dict(op.get("fields") or {})
    if op.get("field"):
        fields[op["field"]] = op.get("value", "")
    if not fields:
        raise ToolError("set_field needs 'field' + 'value' (or 'fields':{...})", code="BAD_OP",
                        fix='{"op":"set_field","ref":"R1","field":"Value","value":"4k7"}')
    insts = _instances(ctx, ref)
    for k, v in fields.items():
        v = "" if v is None else str(v)
        if k == "Footprint" and v:
            _check_footprint(ctx, v)
        if k == "Reference":
            if ctx.sch.find_ref(v):
                raise ToolError(f"reference {v} is already used", code="DUPLICATE_REF")
        for s in insts:
            s.set_field(k, v, hide=op.get("hide") if k not in ("Reference", "Value") else None)
            if k in ("Value", "Reference") and str(sexpr.value(s.node, "fields_autoplaced", "no")) == "yes":
                autoplace_fields(s)
        ctx.changed.append(f"{ref}.{k} = {v!r}")
        if k == "Reference":
            ctx.alias[ref] = v
            for e in ctx.expect:   # connectivity is checked after save, under the new reference
                for key in ("pins", "same"):
                    if key in e:
                        e[key] = [(v if r == ref else r, pn) for r, pn in e[key]]


def _check_footprint(ctx: Ctx, fp: str):
    try:
        libs.read_footprint(fp, ctx.pr.dir if ctx.pr else None)
    except ToolError as e:
        if e.code in ("FOOTPRINT_NOT_FOUND", "LIB_NOT_FOUND", "BAD_FP_ID"):
            raise ToolError(f"footprint '{fp}' does not exist: {e.message}", code="FOOTPRINT_NOT_FOUND",
                            fix=e.fix or "library(action='search_footprints', query=...)", data=e.data)
        ctx.warnings.append(f"could not verify footprint {fp}: {e.message}")


def _pin_state(ctx: Ctx, si: SymInst) -> list[dict]:
    """Snapshot what is attached to each pin so it can be rebuilt after move/swap."""
    out = []
    for p in si.pins_world():
        st = ctx.stub_at(p["xy"])
        it = ctx.items_at(p["xy"], exclude_sym=si)
        rec = {"number": p["number"], "name": p["name"], "xy": p["xy"], "out": p["out"], "stub": None,
               "nc": bool(it["nc"]), "direct": [], "wires": []}
        if st:
            lab = st["labels"][0] if st["labels"] else None
            pw = st["power"][0][0] if st["power"] else None
            rec["stub"] = {"net": lab["net"] if lab else pw.value, "kind": lab["kind"] if lab else "power",
                           "shape": str(sexpr.value(lab["node"], "shape", "bidirectional")) if lab else None}
        for l in it["labels"]:
            rec["direct"].append(("label", l))
        for s, q in it["power"]:
            rec["direct"].append(("power", s))
        for w in it["wires_end"]:
            if st and w["node"] is st["wire"]["node"]:
                continue
            rec["wires"].append(w)
        out.append(rec)
    return out


def _rebuild(ctx: Ctx, si: SymInst, state: list[dict], by="number"):
    newpins = {p["number"]: p for p in si.pins_world()}
    byname = {}
    for p in si.pins_world():
        byname.setdefault(p["name"], p)
    lost = []
    for rec in state:
        p = newpins.get(rec["number"]) if by == "number" else None
        if p is None:
            p = newpins.get(rec["number"]) or (byname.get(rec["name"]) if rec["name"] else None)
        needs = rec["stub"] or rec["nc"] or rec["direct"] or rec["wires"]
        if p is None:
            if needs:
                lost.append(rec)
            continue
        g = {"si": si, "xy": p["xy"], "out": p["out"], "pins": [p], "alternate": None,
             "label": f"{si.ref}.{p['number']}"}
        if rec["stub"]:
            s = rec["stub"]
            _attach(ctx, g, s["net"], "power" if s["kind"] == "power" else "label",
                    s["kind"] if s["kind"] != "power" else "local", s.get("shape") or "bidirectional")
        if rec["nc"]:
            ctx.sch.add_no_connect(p["xy"])
        for kind, obj in rec["direct"]:
            a = sexpr.find(obj["node"] if kind == "label" else obj.node, "at")
            a[1], a[2] = num(p["xy"][0]), num(p["xy"][1])
        for w in rec["wires"]:
            node = w["node"]
            pts = sexpr.find(node, "pts")
            xys = sexpr.find_all(pts, "xy")
            end = xys[0] if _near((float(xys[0][1]), float(xys[0][2])), rec["xy"]) else xys[-1]
            other = xys[-1] if end is xys[0] else xys[0]
            o = (float(other[1]), float(other[2]))
            end[1], end[2] = num(p["xy"][0]), num(p["xy"][1])
            if abs(o[0] - p["xy"][0]) > EPS and abs(o[1] - p["xy"][1]) > EPS:
                # keep it orthogonal: bend at a corner
                corner = [p["xy"][0], o[1]] if p["out"][1] != 0 else [o[0], p["xy"][1]]
                end[1], end[2] = num(corner[0]), num(corner[1])
                ctx.sch.add_wire(p["xy"], corner)
        ctx.touch(p["xy"])
    return lost


def _clear_pin_attachments(ctx: Ctx, state: list[dict]):
    for rec in state:
        if rec["stub"]:
            st = ctx.stub_at(rec["xy"])
            if st:
                ctx.sch.remove(st["wire"]["node"])
                for l in st["labels"]:
                    ctx.sch.remove(l["node"])
                for s, q in st["power"]:
                    ctx.sch.remove(s.node)
        if rec["nc"]:
            for n in ctx.items_at(rec["xy"])["nc"]:
                ctx.sch.remove(n)


def _check_pin_contacts(ctx: Ctx, si: SymInst, ignore: set | None = None, what: str = "placed"):
    """A pin tip landing on another pin, wire end, label or power symbol silently joins nets in KiCad.
    Refuse that (the caller asked for a position, not a connection); a wire merely passing through a pin
    tip does NOT connect in KiCad, so that is only a warning."""
    ignore = ignore or set()
    for p in si.pins_world():
        it = ctx.items_at(p["xy"], exclude_sym=si)
        hits = [f"{s.ref}.{q['number']}" for s, q in it["pins"]]
        hits += [f"power symbol {s.value}" for s, q in it["power"] if id(s.node) not in ignore]
        hits += [f"label {l['net']}" for l in it["labels"] if id(l["node"]) not in ignore]
        hits += ["a wire end" for w in it["wires_end"] if id(w["node"]) not in ignore]
        if hits:
            raise ToolError(f"{si.ref}.{p['number']} {what} at {_fmt(p['xy'])} would touch {', '.join(dict.fromkeys(hits))} "
                            f"and silently join their nets", code="PIN_OVERLAP",
                            fix="pick another 'at' (positions are on the 1.27 mm grid) — or omit 'at' and let Copper place it")
        if any(id(w["node"]) not in ignore for w in it["wires_mid"]):
            ctx.warnings.append(f"{si.ref}.{p['number']} sits on top of a wire at {_fmt(p['xy'])} (not connected in KiCad "
                                f"— looks misleading; move it)")


def op_move(ctx: Ctx, op):
    ref = op.get("ref")
    if not ref:
        raise ToolError("move needs 'ref'", code="BAD_OP")
    insts = _instances(ctx, ref, op.get("unit"))
    if len(insts) > 1 and op.get("unit") is None and op.get("at") is not None:
        raise ToolError(f"{ref} has {len(insts)} units placed — pass 'unit'", code="BAD_OP")
    for si in insts:
        state = _pin_state(ctx, si)
        riders = _run_labels(ctx, si, state)
        _clear_pin_attachments(ctx, state)
        a = sexpr.find(si.node, "at")
        old = si.at
        if op.get("at") is not None:
            new = _xy(op["at"])
        elif op.get("by") is not None:
            d = _xy(op["by"], "by")
            new = [old[0] + d[0], old[1] + d[1]]
        else:
            new = old
        rot = _rot(op["rot"]) if op.get("rot") is not None else si.rot
        a[1:] = [num(new[0]), num(new[1]), rot]
        if "mirror" in op:
            m = _mirror(op.get("mirror"))
            for e in list(si.node):
                if isinstance(e, list) and e and e[0] == "mirror":
                    si.node.remove(e)
            if m:
                idx = si.node.index(a) + 1
                si.node.insert(idx, [Sym("mirror"), Sym(m)])
        autoplace_fields(si)
        keep = {id(w["node"]) for rec in state for w in rec["wires"]}
        keep |= {id(obj["node"] if kind == "label" else obj.node) for rec in state for kind, obj in rec["direct"]}
        _check_pin_contacts(ctx, si, keep, "moved")
        lost = _rebuild(ctx, si, state)
        _reseat_labels(ctx, si, riders)
        ctx.touch(old, new)
        ctx.changed.append(f"moved {si.ref} to {_fmt(new)} rot {rot}" + (f" mirror {si.mirror}" if si.mirror else ""))
        for rec in lost:
            ctx.warnings.append(f"{si.ref}.{rec['number']} lost its connection")


def _run_labels(ctx: Ctx, si: SymInst, state: list[dict]) -> list[tuple]:
    """Labels sitting on the wire runs of a part's wired pins (not on the pin tip or its own stub, which _rebuild
    moves itself): [(pin number, label dict)]. A move re-draws those wires and may leave such a label off them."""
    wires = ctx.sch.wires()
    dots = _dots(ctx)
    out, seen = [], set()
    for rec in state:
        if not rec["wires"]:
            continue
        run, keys = _run_of(wires, rec["xy"], dots)
        st = ctx.stub_at(rec["xy"])
        stub = {id(l["node"]) for l in st["labels"]} if st else set()
        for l in ctx.sch.labels():
            if id(l["node"]) in seen or id(l["node"]) in stub or _near(l["at"], rec["xy"]):
                continue
            if _key(l["at"]) in keys or any(_on_segment(l["at"], wires[i]["a"], wires[i]["b"]) for i in run):
                seen.add(id(l["node"]))
                out.append((rec["number"], l))
    return out


def _reseat_labels(ctx: Ctx, si: SymInst, riders: list[tuple]):
    """Put labels that a move left off their wire back onto the moved pin's wire run (the net keeps its name)."""
    if not riders:
        return
    pins = {p["number"]: p for p in si.pins_world()}
    for number, l in riders:
        p = pins.get(number)
        if p is not None:
            wires = ctx.sch.wires()
            run, keys = _run_of(wires, p["xy"], _dots(ctx))
            if _key(l["at"]) in keys or any(_on_segment(l["at"], wires[i]["a"], wires[i]["b"]) for i in run):
                continue                               # still on this pin's wire run
        ctx.sch.remove(l["node"])                      # off the run, or now on another pin's wire: re-seat it
        if p is None:
            continue
        g = {"si": si, "xy": p["xy"], "out": p["out"], "pins": [p], "alternate": None, "label": f"{si.ref}.{number}"}
        cur, _ = ctx.net_at_pin(p["xy"], si)
        if cur == l["net"]:
            continue                                   # another label already names the run
        shape = str(sexpr.value(l["node"], "shape", "bidirectional"))
        if not _label_on_wire(ctx, g, l["net"], l["kind"], shape):
            _attach(ctx, g, l["net"], "label", l["kind"], shape)
        ctx.changed.append(f"label {l['net']} moved with the wire of {si.ref}.{number}")


def _junctions_cleanup(ctx: Ctx):
    """Drop junctions that no longer join three or more things."""
    for j in list(ctx.sch.items("junction")):
        a = sexpr.find(j, "at")
        q = (float(a[1]), float(a[2]))
        it = ctx.items_at(q)
        if len(it["wires_end"]) + len(it["wires_mid"]) * 2 + len(it["pins"]) < 3:
            ctx.sch.remove(j)


def _prune_dangling(ctx: Ctx, seeds) -> dict:
    """Starting where something was removed, walk back along wire runs whose end now touches nothing and drop them,
    then the labels / power symbols / PWR_FLAGs / no-connect flags left touching nothing. Stops at pins, branch
    points, T-joins and anything still connected — never removes a wire that joins two live items."""
    todo = [tuple(p) for p in seeds]
    done = {"wire": 0, "label": 0, "power": 0}
    guard = 0
    while todo and guard < 5000:
        guard += 1
        p = todo.pop()
        it = ctx.items_at(p)
        if not it["wires_end"] and not it["wires_mid"] and not it["pins"]:
            for l in it["labels"]:
                ctx.sch.remove(l["node"])
                done["label"] += 1
            for s, _ in it["power"]:
                if ctx.sch.remove(s.node):
                    done["power"] += 1
            for n in it["nc"]:
                ctx.sch.remove(n)
            continue
        if len(it["wires_end"]) == 1 and not (it["wires_mid"] or it["pins"] or it["labels"] or it["power"] or it["nc"]):
            w = it["wires_end"][0]
            ctx.sch.remove(w["node"])
            done["wire"] += 1
            todo.append(tuple(w["b"] if _near(w["a"], p) else w["a"]))
            # labels sitting along the removed segment lose their wire too
            todo += [tuple(l["at"]) for l in ctx.sch.labels() if _on_segment(l["at"], w["a"], w["b"])]
            ctx.touch(w["a"], w["b"])
    _junctions_cleanup(ctx)
    return done


def _pruned_msg(d: dict) -> str:
    parts = [f"{n} {k}{'s' if n > 1 else ''}" for k, n in (("wire segment", d["wire"]), ("orphaned label", d["label"]),
                                                            ("orphaned power symbol/flag", d["power"])) if n]
    return (" (also removed " + ", ".join(parts) + ")") if parts else ""


def _dangling_ends(ctx: Ctx) -> list:
    out = []
    for w in ctx.sch.wires():
        for e in (w["a"], w["b"]):
            it = ctx.items_at(e)
            if len(it["wires_end"]) == 1 and not (it["wires_mid"] or it["pins"] or it["labels"] or it["power"] or it["nc"]):
                out.append(e)
    return out


def op_delete(ctx: Ctx, op):
    if op.get("ref"):
        insts = _instances(ctx, op["ref"])
        pruned = {"wire": 0, "label": 0, "power": 0}
        for si in insts:
            state = _pin_state(ctx, si)
            _clear_pin_attachments(ctx, state)
            for rec in state:
                for kind, obj in rec["direct"]:
                    ctx.sch.remove(obj["node"] if kind == "label" else obj.node)
            ctx.sch.remove(si.node)
            ctx.touch(si.at)
            # wires that only served this part's pins go with it (hand-drawn wiring included)
            d = _prune_dangling(ctx, [rec["xy"] for rec in state])
            for k in pruned:
                pruned[k] += d[k]
            left = [f"{si.ref}.{r['number']}" for r in state if any(_near(e, r["xy"]) for e in _dangling_ends(ctx))]
            if left:
                ctx.warnings.append(f"wires left dangling where {', '.join(left)} were — "
                                    f'{{"op":"delete","dangling":true}} removes them')
        ctx.changed.append(f"deleted {insts[0].ref} ({insts[0].lib_id})" + (f", {len(insts)} units" if len(insts) > 1 else "")
                           + _pruned_msg(pruned))
        ctx.sch.prune_lib_symbols()
        return
    if op.get("label"):
        net = str(op["label"]).strip()
        if not any(l["net"] == net for l in ctx.sch.labels()) and net.startswith("/"):
            net = _norm_net(ctx, net)
        kind = (op.get("kind") or "").lower() or None
        if kind not in (None, "local", "global", "hierarchical"):
            raise ToolError(f"kind must be local/global/hierarchical, got {kind}", code="BAD_OP")
        tips = None
        if op.get("pin"):
            groups, _ = ctx.resolve(op["pin"])
            tips = [g["xy"] for g in groups]
        n, kinds, seeds = 0, [], []
        for l in list(ctx.sch.labels()):
            if l["net"] != net or (kind and l["kind"] != kind):
                continue
            if tips is not None:
                st = [w for w in ctx.sch.wires() if _near(w["a"], l["at"]) or _near(w["b"], l["at"])]
                far = [(w["b"] if _near(w["a"], l["at"]) else w["a"]) for w in st]
                if not any(_near(t, l["at"]) or any(_near(t, f_) for f_ in far) for t in tips):
                    continue
            ctx.sch.remove(l["node"])
            seeds.append(l["at"])
            kinds.append(l["kind"])
            n += 1
            ctx.touch(l["at"])
        if not n:
            raise ToolError(f"no {kind + ' ' if kind else ''}label '{net}' found" + (f" at {op['pin']}" if op.get("pin") else ""),
                            code="NOT_FOUND", fix="sch_read(query='labels')")
        d = _prune_dangling(ctx, seeds)
        if "hierarchical" in kinds and not any(l["net"] == net and l["kind"] == "hierarchical" for l in ctx.sch.labels()) \
                and ctx.pr is not None and ctx.sch.path is not None and net in _parent_sheet_pins(ctx.pr, ctx.sch.path):
            ctx.warnings.append(f"removed the last hierarchical label '{net}': the parent sheet's pin '{net}' now connects "
                                f"to nothing (ERC will flag it); pass kind='local' to remove only local labels, or remove the "
                                f"sheet pin in KiCad")
        by = {k: kinds.count(k) for k in dict.fromkeys(kinds)}
        ctx.changed.append(f"deleted {n} label(s) {net}" + (f" ({', '.join(f'{v} {k}' for k, v in by.items())})"
                                                           if len(by) > 1 or "local" not in by else "") + _pruned_msg(d))
        return
    if op.get("wire_at") is not None:
        p = _xy(op["wire_at"], "wire_at")
        hit = [w for w in ctx.sch.wires() if _near(w["a"], p) or _near(w["b"], p) or _on_segment(p, w["a"], w["b"])]
        if not hit:
            near_ = sorted(ctx.sch.wires(), key=lambda w: min(abs(w["a"][0] - p[0]) + abs(w["a"][1] - p[1]),
                                                               abs(w["b"][0] - p[0]) + abs(w["b"][1] - p[1])))[:3]
            raise ToolError(f"no wire at {_fmt(p)}", code="NOT_FOUND",
                            fix="nearest wire ends: " + ", ".join(f"{_fmt(w['a'])}→{_fmt(w['b'])}" for w in near_)
                            if near_ else "this sheet has no wires")
        for w in hit:
            ctx.sch.remove(w["node"])
            ctx.touch(w["a"], w["b"])
        _junctions_cleanup(ctx)
        ctx.changed.append(f"deleted {len(hit)} wire segment(s) at {_fmt(p)}: "
                           + ", ".join(f"{_fmt(w['a'])}→{_fmt(w['b'])}" for w in hit[:4]))
        ends = [e for w in hit for e in (w["a"], w["b"])]
        if any(_near(e, x) for e in ends for x in _dangling_ends(ctx)):
            ctx.warnings.append('the remaining wiring has a dangling end — {"op":"delete","dangling":true} cleans it up')
        return
    if op.get("dangling"):
        d = _prune_dangling(ctx, _dangling_ends(ctx))
        ctx.changed.append(f"removed {d['wire']} dangling wire segment(s)" + _pruned_msg(dict(d, wire=0)))
        return
    raise ToolError("delete needs 'ref', 'label', 'wire_at' [x,y] or 'dangling': true", code="BAD_OP",
                    fix='{"op":"delete","ref":"R3"} · {"op":"delete","label":"NET","kind":"local"} · '
                        '{"op":"delete","wire_at":[50.8,60.96]} · {"op":"delete","dangling":true}')


def op_rename_net(ctx: Ctx, op):
    src = _norm_net(ctx, op.get("from"))
    dst = _norm_net(ctx, op.get("to"))
    if not src or not dst:
        raise ToolError("rename_net needs 'from' and 'to'", code="BAD_OP", fix='{"op":"rename_net","from":"LED_A","to":"LED_RED"}')
    if re.search(r"\s", dst):
        raise ToolError(f"net name '{dst}' contains whitespace", code="BAD_OP")
    if src == dst:
        ctx.changed.append(f"{src} unchanged")
        return
    labels = [l for l in ctx.sch.labels() if l["net"] == src]
    pwr = [s for s in ctx.sch.symbols() if s.is_power and not is_flag(s) and s.value == src]
    if not labels and not pwr:
        raise ToolError(f"no label or power symbol named '{src}' on this sheet", code="NOT_FOUND",
                        fix="sch_read(query='labels') — auto-named nets have no name to rename: connect a pin to the new name")
    if any(l["net"] == dst for l in ctx.sch.labels()) or any(s.is_power and s.value == dst for s in ctx.sch.symbols()):
        raise ToolError(f"net '{dst}' already exists on this sheet — renaming '{src}' to it would merge (short) the two nets",
                        code="NET_CONFLICT", fix="pick an unused name, or connect the pins to that net explicitly if a merge is intended")
    _check_net_name(ctx, dst)
    for l in labels:
        l["node"][1] = dst
        ctx.touch(l["at"])
    ctx.renamed.add(dst)
    ctx.joins.append({"names": [src, dst]})       # the merge check: pins of '{src}' now sit on '{dst}'
    for s in pwr:
        s.set_field("Value", dst)
        autoplace_fields(s)
        ctx.touch(s.at)
    hier = [l for l in labels if l["kind"] == "hierarchical"]
    if hier and ctx.pr is not None and ctx.sch.path is not None and src in _parent_sheet_pins(ctx.pr, ctx.sch.path):
        ctx.warnings.append(f"hierarchical label '{src}' renamed: rename the parent sheet's pin '{src}' to '{dst}' too "
                            f"(KiCad: edit the sheet pin), or ERC reports hier_label_mismatch")
    if any(l["kind"] == "global" for l in labels):
        ctx.warnings.append(f"global label renamed on this sheet only — rename '{src}' on the other sheets too")
    ctx.changed.append(f"renamed net {src} → {dst} ({len(labels)} label(s)" + (f", {len(pwr)} power symbol(s)" if pwr else "") + ")")


def op_disconnect(ctx: Ctx, op):
    for tok in _pins_of(op):
        groups, _ = ctx.resolve(tok)
        for g in groups:
            removed = ctx.remove_attachments(g["xy"])
            n = _remove_wire_run(ctx, g["xy"])
            if n:
                removed.append(f"{n} wire segment(s)")
            if not removed:
                ctx.changed.append(f"{g['label']} had nothing attached")
            else:
                ctx.changed.append(f"disconnected {g['label']}: {', '.join(removed)}")
            ctx.touch(g["xy"])


def _remove_wire_run(ctx: Ctx, tip) -> int:
    """Remove the wire run leaving a pin tip, up to the next pin / junction / label / branch point."""
    removed = 0
    todo = [tip]
    seen = set()
    first = True
    while todo:
        p = todo.pop()
        key = (round(p[0], 3), round(p[1], 3))
        if key in seen:
            continue
        seen.add(key)
        it = ctx.items_at(p)
        if not first:
            stop = it["pins"] or it["labels"] or it["power"] or it["wires_mid"] or len(it["wires_end"]) > 1 or any(
                _near((float(sexpr.find(j, "at")[1]), float(sexpr.find(j, "at")[2])), p) for j in ctx.sch.items("junction"))
            if stop:
                continue
        first = False
        for w in it["wires_end"]:
            ctx.sch.remove(w["node"])
            removed += 1
            todo.append(w["b"] if _near(w["a"], p) else w["a"])
    _junctions_cleanup(ctx)
    return removed


def op_pwr_flag(ctx: Ctx, op):
    net = _norm_net(ctx, op.get("net"))
    if not net:
        raise ToolError("pwr_flag needs 'net'", code="BAD_OP", fix='{"op":"pwr_flag","net":"GND"}')
    _check_net_name(ctx, net)
    # idempotent: a second PWR_FLAG on the same net is an ERC error (two power outputs)
    for s_ in ctx.sch.symbols():
        if is_flag(s_):
            pw = s_.pins_world()
            if pw and ctx.net_at_pin(pw[0]["xy"], s_)[0] == net:
                ctx.changed.append(f"{net} already has PWR_FLAG {s_.ref}")
                return
    flag = libs.read_symbol("power:PWR_FLAG", ctx.pr.dir if ctx.pr else None)
    fkey = ctx.sch.embed(flag)
    if op.get("at") is not None:
        q = _xy(op["at"])
    else:
        area, title = LAY.usable(ctx.sch)
        box = [-3.5, -6.5, 3.5 + text_width(net), 6.5]
        obs = LAY.occupied(ctx.sch) + [title] + ctx.reserved_boxes()
        # next to where the net already lives (its power symbols / labels), connectors first
        targets = []
        for s_ in ctx.sch.symbols():
            if s_.is_power and s_.value == net:
                targets.append(s_.at)
        for l in ctx.sch.labels():
            if l["net"] == net:
                targets.append(l["at"])
        conns = [s_ for s_ in ctx.sch.symbols(include_power=False) if s_.ref[:1] == "J"]
        if conns and targets:
            c0 = conns[0].at
            targets.sort(key=lambda t: (t[0] - c0[0]) ** 2 + (t[1] - c0[1]) ** 2)
        off = None
        for t in targets[:4]:
            off = LAY.find_spot_near(box, [t[0] + 6, t[1]], area, obs, max_r=40)
            if off is not None:
                break
        if off is None:
            off = LAY.find_spot(box, area, obs)
        if off is None:
            raise ToolError("no free room on the sheet for a PWR_FLAG", code="NO_ROOM")
        q = G.snap_pt(off)
    ground = LAY.is_ground(net)
    is_pw = _is_power_net(ctx, net)
    # flag up + GND down; flag down + rail up; non-power net: flag up + label to the right
    fr = 0 if (ground or not is_pw) else 180
    fs = ctx.sch.add_symbol(fkey, flag.lib_id, ctx.sch.next_ref("#FLG"), q, fr)
    if is_pw or ground:
        _place_power_symbol(ctx, net, q, (0, 1) if ground else (0, -1))
    else:
        here = {l["kind"] for l in ctx.sch.labels() if l["net"] == net}
        lk = "local" if ("local" in here or not here) else "global" if "global" in here else "hierarchical"
        ctx.sch.add_wire(q, [q[0] + 2.54, q[1]])
        ctx.sch.add_label(net, [q[0] + 2.54, q[1]], 0, lk,
                          next((str(sexpr.value(l["node"], "shape", "bidirectional")) for l in ctx.sch.labels()
                                if l["net"] == net and l["kind"] == lk and lk != "local"), "bidirectional"))
    ctx.touch(q)
    ctx.changed.append(f"PWR_FLAG {fs.ref} on {net} at {_fmt(q)}")


# ── place / swap ─────────────────────────────────────────────────────────
def _resolve_part(ctx: Ctx, op) -> dict:
    """→ {'lib_id','footprint','value','fields'} from lib_id | lcsc | part (+provider) | spec.

    'lcsc' is the default provider's shorthand; 'part' is any parts provider's part number; 'spec' asks the
    project's providers in order (config parts.providers) and falls back to a generic KiCad symbol."""
    if op.get("lib_id"):
        return {"lib_id": op["lib_id"], "fields": {}}
    if op.get("lcsc"):
        return _from_lcsc(ctx, str(op["lcsc"]).strip().upper())
    if op.get("part"):
        return _from_provider(ctx, str(op["part"]).strip(), op.get("provider"))
    if op.get("spec"):
        spec = str(op["spec"])
        try:
            from ..parts import basic  # noqa: WPS433
        except ImportError:
            basic = None
        names = _provider_names(ctx, op.get("provider")) if basic is not None else []
        tried = []
        for name in names:
            if name != "lcsc":
                r = _spec_from_provider(ctx, spec, name, op)
                if r is not None:
                    return r
                tried.append(name)
                continue
            tried.append("LCSC/JLC")
            pick = None
            try:
                pick = basic.pick(spec)
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                ctx.warnings.append(f"parts.basic.pick failed for '{spec}': {e}")
            if pick and pick.get("lcsc"):
                r = _from_lcsc(ctx, pick["lcsc"])
                if pick.get("value") and not op.get("value"):
                    r["value"] = pick["value"]
                if pick.get("class") and not r["fields"].get("JLC Class"):
                    r["fields"]["JLC Class"] = pick["class"]
                if pick.get("warning"):
                    ctx.warnings.append(f"spec '{spec}': {pick['warning']}")
                return r
        fb = _generic_from_spec(spec)
        if fb is None:
            raise ToolError(f"could not turn spec '{spec}' into a part", code="SPEC_UNRESOLVED",
                            fix="parts_search(query=...) to pick a part, then place it with the op the result gives")
        why = "parts engine unavailable" if basic is None else f"no in-stock {' / '.join(tried) or 'provider'} part matches it"
        after = "set_field LCSC before fab" if "LCSC/JLC" in tried or basic is None else "set its part number before fab"
        ctx.warnings.append(f"spec '{spec}': {why} — placed generic {fb['lib_id']} {fb['value']}; "
                            f"pick one with parts_search(query='{spec}') and {after}")
        return fb
    raise ToolError("place needs 'lib_id', 'lcsc', 'part' or 'spec'", code="BAD_OP",
                    fix='{"op":"place","lib_id":"Device:R","value":"10k","footprint":"Resistor_SMD:R_0603_1608Metric"} '
                        'or {"op":"place","spec":"10k 0603"} or {"op":"place","lcsc":"C25804"}')


def _provider_names(ctx: Ctx, provider=None) -> list[str]:
    """The parts providers a spec is resolved through, in order (the project's config; 'lcsc' by default)."""
    if provider:
        return [str(provider).strip().lower()]
    from .. import config
    return config.load(ctx.pr).providers


def _part_dict(r: dict) -> dict:
    return {"lib_id": r["lib_id"], "footprint": r.get("footprint"), "value": r.get("value"),
            "fields": dict(r.get("fields") or {}), "basic": r.get("basic"), "part_field": r.get("part_field")}


def _spec_from_provider(ctx: Ctx, spec: str, name: str, op) -> dict | None:
    from ..parts import providers as PV
    r, warns, _tried = PV.resolve_spec(ctx.pr, spec, provider=name)
    ctx.warnings.extend(warns)
    return _part_dict(r) if r is not None else None


def _from_provider(ctx: Ctx, part: str, provider=None) -> dict:
    """A parts provider's part number → placeable part ({"op":"place","part":"…","provider":"local"})."""
    from ..parts import providers as PV
    r = PV.resolve_part(ctx.pr, part, provider)
    for w in r.get("warnings") or []:
        ctx.warnings.append(f"part '{part}': {w}")
    return _part_dict(r)


def _from_lcsc(ctx: Ctx, lcsc: str) -> dict:
    if not re.fullmatch(r"C\d{2,9}", lcsc):
        raise ToolError(f"'{lcsc}' is not an LCSC part number (C followed by digits)", code="BAD_LCSC")
    try:
        from ..parts import importer  # noqa: WPS433
    except ImportError as e:
        raise ToolError(f"LCSC import is unavailable ({e})", code="PARTS_UNAVAILABLE",
                        fix="place with an explicit lib_id + footprint and set_field LCSC=" + lcsc)
    r = importer.resolve_lcsc(ctx.pr, lcsc)
    fields = dict(r.get("fields") or {})
    fields.setdefault("LCSC", r.get("lcsc", lcsc))
    return {"lib_id": r["lib_id"], "footprint": r.get("footprint"), "value": r.get("value"), "fields": fields,
            "basic": r.get("basic")}


_SPEC_FP = {
    "R": "Resistor_SMD:R_{p}_{m}Metric", "C": "Capacitor_SMD:C_{p}_{m}Metric", "L": "Inductor_SMD:L_{p}_{m}Metric",
    "LED": "LED_SMD:LED_{p}_{m}Metric",
}
_METRIC = {"0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216", "1210": "3225"}


def _generic_from_spec(spec: str) -> dict | None:
    s = spec.strip()
    pkg = next((p for p in _METRIC if re.search(rf"\b{p}\b", s)), "0603")
    low = s.lower()
    if "led" in low:
        color = next((c for c in ("red", "green", "blue", "yellow", "white", "orange") if c in low), "")
        return {"lib_id": "Device:LED", "value": f"LED {color}".strip(),
                "footprint": _SPEC_FP["LED"].format(p=pkg, m=_METRIC[pkg]), "fields": {}}
    m = re.search(r"(\d+(?:\.\d+)?\s*[pnuµm]?\s*F\b|\d+[pnuµ]\d*F?\b)", s, re.I)
    if m and re.search(r"[pnuµ]|F", m.group(1)):
        return {"lib_id": "Device:C", "value": m.group(1).replace(" ", ""),
                "footprint": _SPEC_FP["C"].format(p=pkg, m=_METRIC[pkg]), "fields": {}}
    m = re.search(r"(\d+(?:\.\d+)?\s*[pnuµm]H)\b", s, re.I)
    if m:
        return {"lib_id": "Device:L", "value": m.group(1).replace(" ", ""),
                "footprint": _SPEC_FP["L"].format(p=pkg, m=_METRIC[pkg]), "fields": {}}
    m = re.search(r"\b(\d+(?:\.\d+)?[kKmMR]?\d*)\s*(?:ohm|Ω|r)?\b", s)
    if m:
        return {"lib_id": "Device:R", "value": m.group(1), "footprint": _SPEC_FP["R"].format(p=pkg, m=_METRIC[pkg]),
                "fields": {}}
    return None


_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
       "R": 1.0, "r": 1.0, "": 1.0}


def si_value(v) -> float | None:
    """'27R'/'27'/'27Ω' → 27, '4k7' → 4700, '2.2k' → 2200, '100nF'/'100n' → 1e-7, '4.7uH' → 4.7e-6; else None."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([pnuµmkKMGRr]?)(\d*)\s*(?:F|H|Ω|ohms?|Ohms?)?\s*", str(v or ""))
    if not m or (m.group(3) and "." in m.group(1)):
        return None
    num_ = float(m.group(1) + ("." + m.group(3) if m.group(3) else ""))
    return num_ * _SI[m.group(2)]


def _value_with_unit(value, prefix: str):
    """A resistor value from the parts catalogue comes as a bare number ('27'): keep the unit ('27R') so the BOM
    comment and the sheet stay unambiguous."""
    v = str(value or "")
    if prefix.upper() in ("R", "RN") and re.fullmatch(r"\d+(?:\.\d+)?", v):
        return v + "R"
    return value


_CONN_FP = re.compile(r"^(?:HDR|CONN|USB[-_]|JST|FPC|FFC|IDC|TERMINAL|KF\d|DC-?JACK|RJ\d|SIM[-_]|TF[-_]|SD[-_]?CARD|"
                      r"BAT[-_]?H|PinHeader|PinSocket|Connector)", re.I)
_CONN_DESC = re.compile(r"\b(?:pin headers?|female headers?|board to board|wire to board|connectors?|terminal blocks?)\b", re.I)


def _looks_like_connector(lib, part: dict) -> bool:
    """EasyEDA symbols of headers/connectors often carry the prefix 'U': recognise them by footprint / description."""
    fp = str(part.get("footprint") or lib.properties.get("Footprint") or "").split(":")[-1]
    desc = str((part.get("fields") or {}).get("Description") or lib.properties.get("Description") or "")
    if _CONN_FP.match(fp):
        return True
    return bool(_CONN_DESC.search(desc)) and not re.search(r"\b(?:ESD|TVS|protection|driver|controller|transceiver)\b",
                                                          desc, re.I)


class PlaceSpec:
    def __init__(self, op, idx):
        self.op = op
        self.idx = idx
        self.lib = None
        self.part = None
        self.ref = None
        self.units = [1]
        self.key = None


def _prepare_places(ctx: Ctx, ops) -> list[PlaceSpec]:
    taken = ctx.sch.used_refs()
    on_sheet = {s.ref for s in ctx.sch.symbols()}
    specs = []
    ids: dict = {}
    for i, op in enumerate(ops):
        if op["op"] != "place":
            continue
        if op.get("id") is not None:
            oid = str(op["id"]).strip()
            if not oid or "." in oid or re.search(r"\s", oid):
                raise ToolError(f"ops[{i}] (place): id {op['id']!r} must be a non-empty word without '.' or spaces "
                                f"(it is used as 'ID.PIN')", code="BAD_OP", fix='e.g. "id":"r_pullup" → pins "r_pullup.1"')
            if oid in ids:
                raise ToolError(f"ops[{i}] (place): id '{oid}' is already used by ops[{ids[oid]}]", code="DUPLICATE_ID",
                                fix="give every placed part its own id")
            if oid in on_sheet and str(op.get("ref") or "") != oid:
                raise ToolError(f"ops[{i}] (place): id '{oid}' is already a reference on this sheet — pins '{oid}.x' "
                                f"would be ambiguous", code="DUPLICATE_ID",
                                fix="use a descriptive id such as 'r_led' (the result's 'refs' maps it to the new reference)")
            ids[oid] = i
        ps = PlaceSpec(op, i)
        ps.part = _resolve_part(ctx, op)
        ps.lib = libs.read_symbol(ps.part["lib_id"], ctx.pr.dir if ctx.pr else None)
        nunits = ps.lib.unit_count
        u = op.get("unit")
        if u in (None, "all", "ALL"):
            ps.units = list(range(1, nunits + 1)) if (u is not None or nunits > 1) else [1]
        else:
            try:
                ui = int(u)
            except (TypeError, ValueError):
                raise ToolError(f"unit must be an integer or 'all', got {u!r}", code="BAD_OP")
            if not 1 <= ui <= nunits:
                raise ToolError(f"{ps.lib.lib_id} has {nunits} unit(s); unit {ui} is invalid", code="BAD_OP")
            ps.units = [ui]
        prefix = (ps.lib.properties.get("Reference") or "U").rstrip("?") or "U"
        if prefix == "U" and _looks_like_connector(ps.lib, ps.part):
            prefix = "J"
        want = op.get("ref")
        if want and not str(want).endswith("?"):
            want = str(want)
            if want in taken:
                existing = ctx.sch.find_ref(want)
                placed_units = {s.unit for s in existing}
                if existing and existing[0].lib_id == ps.lib.lib_id and not (placed_units & set(ps.units)):
                    pass  # adding further units of an existing multi-unit part
                else:
                    raise ToolError(f"reference {want} already exists", code="DUPLICATE_REF",
                                    fix="omit 'ref' to auto-annotate, or use move/set_field/swap on the existing part")
            ps.ref = want
        else:
            if want:
                prefix = str(want).rstrip("?") or prefix
            if ps.lib.is_power:
                prefix = "#PWR"
            ps.ref = ctx.sch.next_ref(prefix, taken)
        taken.add(ps.ref)
        ps.key = str(op.get("id") or op.get("ref") or f"#{i}")
        if op.get("id"):
            ctx.alias[str(op["id"])] = ps.ref
        ctx.refs[ps.key] = ps.ref
        if op.get("ref") and str(op["ref"]).endswith("?"):
            ctx.alias.setdefault(str(op["ref"]), ps.ref)
        specs.append(ps)
    return specs


def _planned_nets(ctx: Ctx, ops, specs) -> tuple[dict, dict]:
    """Which pins of new parts will get which nets (for layout), plus existing-part pins per net."""
    by_ref = {ps.ref: ps for ps in specs}
    plan: dict[str, dict] = {}
    ncs: dict[str, set] = {}
    existing_net_pins: dict[str, list] = {}
    for op in ops:
        if op["op"] in ("connect", "power", "no_connect", "label") and (op.get("pin") or op.get("pins")):
            for tok in _pins_of(op):
                try:
                    ref, pin = ctx.split_pin(tok)
                except ToolError:
                    continue
                ps = by_ref.get(ref)
                if ps is None:
                    if op["op"] != "no_connect" and op.get("net"):
                        insts = ctx.sch.find_ref(ref)
                        if insts:
                            hits, _ = G.match_pins(insts[0].lib_pins(), pin)
                            for h in hits:
                                existing_net_pins.setdefault(op["net"], []).append((ref, h["number"]))
                    continue
                hits, _ = G.match_pins(ps.lib.pins, pin)
                for h in hits:
                    if op["op"] == "no_connect":
                        ncs.setdefault(ps.ref, set()).add(h["number"])
                    else:
                        plan.setdefault(ps.ref, {})[h["number"]] = str(op.get("net"))
        elif op["op"] == "wire" and op.get("from") and op.get("to"):
            pair = []
            for tok in (op["from"], op["to"]):
                try:
                    pair.append(ctx.split_pin(tok))
                except ToolError:
                    pass
            if len(pair) == 2:
                net = f"__W_{pair[0][0]}_{pair[0][1]}"
                for ref, pin in pair:
                    ps = by_ref.get(ref)
                    if ps:
                        hits, _ = G.match_pins(ps.lib.pins, pin)
                        for h in hits:
                            plan.setdefault(ps.ref, {})[h["number"]] = net
    # existing pins already on a net (labels/power at their stubs) help satellites find them
    if any(op["op"] == "place" and op.get("at") is None for op in ops) and ctx.sch.symbols(include_power=False):
        # KiCad's own netlist: one ~0.3 s export instead of a geometric walk per pin (minutes on big sheets)
        nl = None
        if ctx.pr is not None and ctx.sch.path is not None and ctx.sch.path.exists() and ctx.pr.sch.exists():
            try:
                from .netlist import export_netlist, pin_nets, is_unconnected
                nl = pin_nets(export_netlist(ctx.pr))
            except ToolError:
                nl = None
        if nl is not None:
            here = {s.ref for s in ctx.sch.symbols(include_power=False)}
            for (ref, pin), net in nl.items():
                if ref in here and not is_unconnected(net) and not net.startswith("Net-("):
                    existing_net_pins.setdefault(net.split("/")[-1], []).append((ref, pin))
        else:
            for si in ctx.sch.symbols(include_power=False):
                for p in si.pins_world():
                    net, how = ctx.net_at_pin(p["xy"], si)
                    if net:
                        existing_net_pins.setdefault(net, []).append((si.ref, p["number"]))
    return {"nets": plan, "nc": ncs}, existing_net_pins


def _do_places(ctx: Ctx, specs: list[PlaceSpec], plan, existing_net_pins):
    """Embed + position + instantiate all new parts (auto-layout for those without 'at')."""
    auto = []
    for ps in specs:
        if ps.op.get("at") is None:
            for u in ps.units:
                auto.append(LAY.Item(key=f"{ps.ref}#{u}", libsym=ps.lib, unit=u,
                                     rot=_rot(ps.op["rot"]) if ps.op.get("rot") is not None else None,
                                     mirror=_mirror(ps.op.get("mirror")), group=ps.op.get("group"),
                                     near=ctx.alias.get(ps.op.get("near"), ps.op.get("near")),
                                     value=str(ps.op.get("value") or ps.part.get("value") or ps.lib.properties.get("Value", "")),
                                     ref=ps.ref, nets=dict(plan["nets"].get(ps.ref, {})),
                                     nc=set(plan["nc"].get(ps.ref, set()))))
    # layout keys must be unique; make near references point at the first unit key
    keymap = {}
    for it in auto:
        keymap.setdefault(it.ref, it.key)
    for it in auto:
        if it.near in keymap:
            it.near = keymap[it.near]
    positions = {}
    if auto:
        try:
            positions = LAY.arrange(ctx.sch, auto, ctx.pnames, existing_net_pins)
        except RuntimeError as e:
            raise ToolError(str(e), code="NO_ROOM", fix="split the design into hierarchical sheets")
        if positions.get("__paper__"):
            ctx.sch.set_paper(positions["__paper__"])
            ctx.changed.append(f"page size → {positions['__paper__']}")
        for il in positions.get("__inline__", []):
            ent = dict(il, done=False)
            ctx.inline[tuple(il["a"])] = ent
            ctx.inline[tuple(il["b"])] = ent
    for ps in specs:
        op = ps.op
        key = ctx.sch.embed(ps.lib)
        value = op.get("value") or _value_with_unit(ps.part.get("value"), re.sub(r"\d+$", "", ps.ref))
        fields = dict(ps.part.get("fields") or {})
        fields.update({k: str(v) for k, v in (op.get("fields") or {}).items()})
        fp = op.get("footprint") or ps.part.get("footprint")
        if fp:
            _check_footprint(ctx, fp)
            fields["Footprint"] = fp
        elif not ps.lib.properties.get("Footprint") and not ps.lib.is_power:
            ctx.warnings.append(f"{ps.ref} ({ps.lib.lib_id}) has no footprint — set_field Footprint before PCB")
        for i, u in enumerate(ps.units):
            if op.get("at") is not None:
                at = _xy(op["at"])
                if i:
                    bb = ps.lib.bbox(ps.units[i - 1])
                    at = [at[0] + (bb[2] - bb[0] + 10.16) * i, at[1]]
                    at = G.snap_pt(at)
                rot = _rot(op.get("rot"))
            else:
                pos = positions.get(f"{ps.ref}#{u}")
                if pos is None:
                    raise ToolError(f"auto-layout found no spot for {ps.ref} (unit {u})", code="LAYOUT_FAILED",
                                    fix=f"give that place op an explicit 'at':[x,y] (mm) or drop its 'near'")
                at, rot = pos["at"], pos["rot"]
            si = ctx.sch.add_symbol(key, ps.lib.lib_id, ps.ref, at, rot, _mirror(op.get("mirror")), u,
                                    value=str(value) if value else None, fields=fields, dnp=bool(op.get("dnp")),
                                    body_style=int(op.get("body_style") or 1))
            if op.get("at") is not None and not ps.lib.is_power:
                _check_pin_contacts(ctx, si)
            bb = si.body_bbox()
            ctx.region.append(bb)
            pf = ps.part.get("part_field") or "LCSC"          # the provider's part-number field (LCSC by default)
            src = ps.part.get("fields", {}).get(pf) or fields.get(pf)
            jclass = fields.get("JLC Class") if pf == "LCSC" and fields.get("LCSC") == src else None
            also = [] if i else [str(sexpr.value(p, "reference", "")) for p in sexpr.find_all(
                sexpr.find(sexpr.find(si.node, "instances") or [], "project") or [], "path") if str(p[1]) != ctx.sch.sheet_path]
            ctx.changed.append(f"placed {ps.ref}{'' if len(ps.units) == 1 else f' unit {u}'} {ps.lib.lib_id}"
                               + (f" {value}" if value else "") + f" at {_fmt(at)}" + (f" rot {rot}" if rot else "")
                               + (f" {pf} {src}" if src else "")
                               + (f" (JLC {jclass})" if src and jclass else "")
                               + (f" (other sheet instance(s): {', '.join(also)})" if also and not ps.lib.is_power else ""))
        # warn when another symbol overlaps an explicitly positioned part
        if op.get("at") is not None:
            for other in ctx.sch.symbols():
                if other.ref == ps.ref or other.is_power:
                    continue
                if LAY._rect_hit(other.body_bbox(), bb, -0.1):
                    ctx.warnings.append(f"{ps.ref} overlaps {other.ref}")
                    break


def op_swap(ctx: Ctx, op):
    ref = op.get("ref")
    if not ref:
        raise ToolError("swap needs 'ref'", code="BAD_OP")
    insts = _instances(ctx, ref)
    part = None
    if op.get("lcsc"):
        part = _from_lcsc(ctx, str(op["lcsc"]).upper())
    elif op.get("part"):
        part = _from_provider(ctx, str(op["part"]).strip(), op.get("provider"))
    elif op.get("lib_id"):
        part = {"lib_id": op["lib_id"], "fields": {}}
    if part is None and not op.get("footprint"):
        raise ToolError("swap needs 'lib_id', 'lcsc', 'part' and/or 'footprint'", code="BAD_OP")
    if part is not None:
        lib = libs.read_symbol(part["lib_id"], ctx.pr.dir if ctx.pr else None)
        key = ctx.sch.embed(lib)
        old_lib = insts[0].lib_id
        old_default = (ctx.sch.lib_symbol(insts[0].lib_name) and
                       {str(p[1]): str(p[2]) for p in sexpr.find_all(ctx.sch.lib_symbol(insts[0].lib_name), "property") if len(p) > 2}.get("Value"))
        for si in insts:
            state = _pin_state(ctx, si)
            _clear_pin_attachments(ctx, state)
            sexpr.set_value(si.node, "lib_id", lib.lib_id)
            for e in list(si.node):
                if isinstance(e, list) and e and e[0] == "lib_name":
                    si.node.remove(e)
            for e in list(si.node):
                if isinstance(e, list) and e and e[0] == "pin":
                    si.node.remove(e)
            idx = next(i for i, e in enumerate(si.node) if isinstance(e, list) and e and e[0] == "instances")
            for p in lib.unit_pins(si.unit):
                if not any(isinstance(e, list) and e and e[0] == "pin" and str(e[1]) == p["number"] for e in si.node):
                    si.node.insert(idx, [Sym("pin"), p["number"], [Sym("uuid"), new_uuid()]])
                    idx += 1
            new_val = _value_with_unit(part.get("value"), re.sub(r"\d+$", "", si.ref))
            old_n, new_n = si_value(si.value), si_value(new_val)
            if old_n is not None and new_n is not None and abs(old_n - new_n) <= 1e-9 * max(abs(old_n), 1e-15):
                pass                      # same value written differently ('27R' vs '27'): keep the designer's form
            elif si.value == old_default or not si.value:
                si.set_field("Value", new_val or lib.properties.get("Value", ""))
            elif new_val:
                si.set_field("Value", new_val)
            fp = op.get("footprint") or part.get("footprint") or lib.properties.get("Footprint", "")
            if fp:
                _check_footprint(ctx, fp)
                si.set_field("Footprint", fp)
            for k, v in (part.get("fields") or {}).items():
                si.set_field(k, str(v))
            ctx.sch._pin_cache.clear()
            autoplace_fields(si)
            lost = _rebuild(ctx, si, state, by="number")
            for rec in lost:
                ctx.warnings.append(f"{ref}.{rec['number']} ({rec['name']}) has no counterpart on {lib.lib_id}; its connection was dropped")
        ctx.sch.prune_lib_symbols()
        ctx.changed.append(f"swapped {ref}: {old_lib} → {lib.lib_id}")
    else:
        _check_footprint(ctx, op["footprint"])
        for si in insts:
            si.set_field("Footprint", op["footprint"])
        ctx.changed.append(f"{ref}.Footprint = {op['footprint']}")


DISPATCH = {
    "connect": op_connect,
    "power": lambda c, o: op_connect(c, o, force_power=True),
    "no_connect": op_no_connect,
    "wire": op_wire,
    "label": op_label,
    "set_field": op_set_field,
    "move": op_move,
    "delete": op_delete,
    "rename_net": op_rename_net,
    "disconnect": op_disconnect,
    "swap": op_swap,
    "text": op_text,
    "pwr_flag": op_pwr_flag,
}


def _normalize(ops) -> list[dict]:
    if not isinstance(ops, list) or not ops:
        raise ToolError("'ops' must be a non-empty array of op objects", code="BAD_ARGUMENT",
                        fix='[{"op":"place","lib_id":"Device:R","value":"10k"}, {"op":"connect","pin":"R1.1","net":"+3V3"}]')
    out = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or not op.get("op"):
            raise ToolError(f"ops[{i}] must be an object with an 'op' field", code="BAD_OP")
        name = str(op["op"]).lower()
        if name not in OP_FIELDS:
            raise ToolError(f"ops[{i}]: unknown op '{op['op']}'", code="BAD_OP",
                            fix=f"valid ops: {', '.join(OP_FIELDS)}")
        extra = set(op) - OP_FIELDS[name] - {"op", "sheet", "comment"}
        if extra:
            raise ToolError(f"ops[{i}] ({name}): unknown field(s) {sorted(extra)}; valid: {sorted(OP_FIELDS[name])}",
                            code="BAD_OP")
        out.append(dict(op, op=name))
    return out


# ── hierarchy (fast regex scan, cached by file mtime — big designs have 30+ sheets) ──────────
_SHEET_BLOCK = re.compile(r'^\t\(sheet\n(.*?)^\t\)', re.M | re.S)
_ROOT_UUID = re.compile(r'^\t\(uuid "([^"]+)"\)', re.M)
_BLOCK_UUID = re.compile(r'^\t\t\(uuid "([^"]+)"\)', re.M)
_BLOCK_PROP = re.compile(r'^\t\t\(property "(Sheetfile|Sheet file|Sheetname|Sheet name)" "((?:[^"\\]|\\.)*)"', re.M)
_BLOCK_PIN = re.compile(r'^\t\t\(pin "((?:[^"\\]|\\.)*)"', re.M)
_REF_RE = re.compile(r'\((?:reference|property "Reference") "([^"]+)"')
_scan_cache: dict = {}


def _scan_file(f: Path) -> dict | None:
    """{'uuid', 'sheets': [{'file','name','uuid','pins'}], 'refs': set} for one .kicad_sch (None if unreadable)."""
    try:
        st = f.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    c = _scan_cache.get(str(f))
    if c and c[0] == key:
        return c[1]
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _ROOT_UUID.search(text)
    blocks = _SHEET_BLOCK.findall(text)
    info = None
    if m and (blocks or "(sheet\n" not in text and "(sheet " not in text):
        sheets = []
        for blk in blocks:
            props = {k: sexpr._unescape(v) for k, v in _BLOCK_PROP.findall(blk)}
            u = _BLOCK_UUID.search(blk)
            sheets.append({"file": props.get("Sheetfile") or props.get("Sheet file", ""),
                           "name": props.get("Sheetname") or props.get("Sheet name", ""),
                           "uuid": u.group(1) if u else "", "pins": {sexpr._unescape(x) for x in _BLOCK_PIN.findall(blk)}})
        info = {"uuid": m.group(1), "sheets": sheets}
    else:   # not KiCad-canonical formatting → full parse
        try:
            tree = sexpr.loads(text)
        except ValueError:
            return None
        sheets = []
        for sh in sexpr.find_all(tree, "sheet"):
            props = {str(p[1]): str(p[2]) for p in sexpr.find_all(sh, "property") if len(p) > 2}
            sheets.append({"file": props.get("Sheetfile") or props.get("Sheet file", ""),
                           "name": props.get("Sheetname") or props.get("Sheet name", ""),
                           "uuid": str(sexpr.value(sh, "uuid", "")),
                           "pins": {str(p[1]) for p in sexpr.find_all(sh, "pin") if len(p) > 1}})
        info = {"uuid": str(sexpr.value(tree, "uuid", "")), "sheets": sheets}
    info["refs"] = {x for x in _REF_RE.findall(text)}
    if len(_scan_cache) > 400:
        _scan_cache.clear()
    _scan_cache[str(f)] = (key, info)
    return info


def hierarchy(pr) -> list[dict]:
    """Every sheet INSTANCE: [{'file': Path, 'path': '/root-uuid/…', 'name': sheet name, 'parent': Path|None,
    'pins': sheet-pin names on the parent's sheet symbol}] — root first, breadth-first."""
    out = []
    if not pr.sch.exists():
        return out
    root = _scan_file(pr.sch)
    if root is None:
        return out
    out.append({"file": pr.sch, "path": f"/{root['uuid']}", "name": "", "parent": None, "pins": set()})
    todo = [(pr.sch, f"/{root['uuid']}", 0)]
    while todo and len(out) < 2000:
        f, path, depth = todo.pop(0)
        info = _scan_file(f)
        if info is None or depth > 32:
            continue
        for sh in info["sheets"]:
            child = f.parent / sh["file"].replace("\\", "/")
            sp = f"{path}/{sh['uuid']}"
            out.append({"file": child, "path": sp, "name": sh["name"], "parent": f, "pins": sh["pins"],
                        "sfile": sh["file"].replace("\\", "/")})
            if child.exists():
                todo.append((child, sp, depth + 1))
    return out


def sheet_files(pr) -> list[Path]:
    seen, out = set(), []
    for h in hierarchy(pr):
        k = str(h["file"]).lower()
        if k not in seen and h["file"].exists():
            seen.add(k)
            out.append(h["file"])
    return out


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


def sheet_file(pr, sheet: str | None) -> tuple[Path, str | None]:
    """Resolve op['sheet'] (file name / sheet name / path, case-insensitive) → (file, instance path)."""
    if not sheet:
        return pr.sch, None
    want = str(sheet).strip()
    wl = want.replace("\\", "/").lower()
    want_abs = Path(want) if Path(want).is_absolute() else pr.dir / want
    if wl in ("/", "root") or (pr.sch.exists() and want_abs.exists() and _same(want_abs, pr.sch)):
        return pr.sch, None
    hs = hierarchy(pr)
    for h in hs[1:]:
        sf = h["sfile"]
        names = {sf.lower(), h["name"].lower(), h["file"].name.lower(), h["file"].stem.lower()}
        if wl in names or (want_abs.exists() and h["file"].exists() and _same(h["file"], want_abs)):
            return h["file"], h["path"]
    avail = sorted({f"{h['file'].name}" + (f" (sheet '{h['name']}')" if h["name"] else "") for h in hs[1:]})
    raise ToolError(f"sheet '{sheet}' not found in the hierarchy of {pr.sch.name}", code="SHEET_NOT_FOUND",
                    fix=("sub-sheets: " + ", ".join(avail[:20]) + " — omit 'sheet' for the root") if avail else
                    "this project has no sub-sheets — omit 'sheet' to edit the root sheet "
                    "(Copper cannot create new hierarchical sheets yet: use global labels, or add the sheet in KiCad)")


def _hierarchy_context(pr, f: Path) -> tuple[set, list]:
    """(references used anywhere in the project, every instance path of sheet file f)."""
    refs: set = set()
    paths: list = []
    hs = hierarchy(pr)
    seen = set()
    for h in hs:
        k = str(h["file"]).lower()
        if k not in seen:
            seen.add(k)
            info = _scan_file(h["file"]) if h["file"].exists() else None
            if info:
                refs |= info["refs"]
        if h["parent"] is not None and h["file"].exists() and _same(h["file"], f):
            paths.append(h["path"])
    return refs, paths


def _parent_sheet_pins(pr, f: Path) -> set:
    """Names of the sheet pins on every sheet symbol (anywhere in the hierarchy) that instantiates file f."""
    pins: set = set()
    for h in hierarchy(pr):
        if h["parent"] is not None and h["file"].exists() and _same(h["file"], f):
            pins |= h["pins"]
    return pins


def load_sheet(pr, sheet: str | None = None, create: bool = True) -> Schematic:
    f, spath = sheet_file(pr, sheet)
    if f.exists():
        s = Schematic.load(f, pr.name, spath)
        refs, paths = _hierarchy_context(pr, f)
        s.extra_refs = refs
        s.other_paths = [p for p in paths if p != s.sheet_path]
        return s
    if not create:
        raise ToolError(f"{f.name} does not exist", code="NOT_FOUND", fix="sch_apply with place ops creates it")
    return Schematic.new(f, pr.name)


def _guard(pr, f: Path, while_open: str):
    """Refuse to edit a sheet KiCad has open. eeschema locks only the ROOT file of an open hierarchy
    (~root.kicad_sch.lck), so a sub-sheet edit must check the root's lock as well."""
    P.guard_writable(f, while_open)
    try:
        sub = pr.sch.exists() and f.resolve() != pr.sch.resolve()
    except OSError:
        sub = False
    if sub and while_open == "refuse" and P.is_open_in_kicad(pr.sch):
        raise ToolError(f"{pr.sch.name} (the root of the hierarchy containing {f.name}) is open in KiCad's schematic "
                        f"editor; editing {f.name} now would be overwritten when KiCad saves.", code="FILE_OPEN_IN_KICAD",
                        fix="Save & close the schematic editor in KiCad, then retry — or retry with while_open='revert' "
                            "and use File ▸ Revert in KiCad afterwards.")


def _refs_of_path(sch: Schematic, path: str) -> set:
    out = set()
    for n in sexpr.find_all(sch.tree, "symbol"):
        insts = sexpr.find(n, "instances")
        for proj in sexpr.find_all(insts or [], "project"):
            for p in sexpr.find_all(proj, "path"):
                if str(p[1]) == path:
                    out.add(str(sexpr.value(p, "reference", "")))
    return out


def _op_refs(ops) -> set:
    """References named by ops (not temporary ids of parts placed in the same batch)."""
    ids = {str(op.get("id")) for op in ops if op["op"] == "place" and op.get("id")}
    refs = set()
    for op in ops:
        if op["op"] in ("set_field", "move", "delete", "swap") and op.get("ref"):
            refs.add(str(op["ref"]))
        toks = []
        if op["op"] in ("connect", "power", "no_connect", "disconnect", "label") and (op.get("pin") or op.get("pins")):
            try:
                toks = _pins_of(op)
            except ToolError:
                toks = []
        toks += [op[k] for k in ("from", "to") if op["op"] == "wire" and op.get(k)]
        refs |= {str(x).split(".", 1)[0] for x in toks if "." in str(x)}
    return refs - ids


def _pick_instance(sch: Schematic, ops) -> str | None:
    """A sheet used several times: when the ops name references of ANOTHER instance (R302 while editing via
    'ampli_ht.kicad_sch', whose first instance says R202), edit through that instance. Returns its path."""
    others = list(getattr(sch, "other_paths", []) or [])
    if not others:
        return None
    want = _op_refs(ops)
    here = _refs_of_path(sch, sch.sheet_path)
    missing = {r for r in want if r not in here}
    if not missing:
        return None
    for path in others:
        if missing <= _refs_of_path(sch, path):
            sch.other_paths = [p for p in [sch.sheet_path] + others if p != path]
            sch.sheet_path = path
            return path
    return None


JOINING_OPS = {"connect", "power", "wire", "label", "move", "swap", "pwr_flag"}


def _auto_net(name: str) -> bool:
    return name.startswith("Net-(") or name.startswith("unconnected-(")


def unintended_merges(before: dict, after: dict, asked: list[dict]) -> list[dict]:
    """Nets in ``after`` that join pins which were apart in ``before`` without the batch asking for it.

    Pins are grouped by what was already connected (``before``), by the joins the batch asked for (``asked``:
    expectations {'same': pins} / {'pins', 'net'} and explicit {'pins', 'names'}) and by net name. A net in
    ``after`` that spans two groups was joined by accident. Pins the batch never mentioned and that did not exist
    before (new parts' hidden power pins, for instance) are not judged."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(xs):
        xs = list(xs)
        for x in xs[1:]:
            parent[find(x)] = find(xs[0])

    def pin(ref, num_):
        return ("p", str(ref), str(num_))

    tracked = set()
    for net, nodes in before.get("nets", {}).items():
        ps = [pin(n["ref"], n["pin"]) for n in nodes if "?" not in n["ref"]]
        tracked.update(ps)
        union(ps + ([("n", net.rsplit("/", 1)[-1])] if ps and not _auto_net(net) else []))
    for e in asked:
        ps = [pin(*k) for k in (e.get("same") or e.get("pins") or [])]
        names = ([e["net"]] if e.get("net") else []) + list(e.get("names") or [])
        tracked.update(ps)
        union(ps + [("n", str(n)) for n in names])
    bad = []
    for net, nodes in after.get("nets", {}).items():
        groups: dict = {}
        for n in nodes:
            p = pin(n["ref"], n["pin"])
            if p in tracked:
                groups.setdefault(find(p), []).append(f"{p[1]}.{p[2]}")
        if len(groups) > 1:
            bad.append({"net": net, "groups": sorted(sorted(g) for g in groups.values())})
    return bad


def unintended_splits(before: dict, after: dict) -> list[dict]:
    """Nets of ``before`` whose pins (those still present) end up on more than one net in ``after``."""
    where = {}
    for net, nodes in after.get("nets", {}).items():
        for n in nodes:
            where[(str(n["ref"]), str(n["pin"]))] = net
    bad = []
    for net, nodes in before.get("nets", {}).items():
        groups: dict = {}
        for n in nodes:
            k = (str(n["ref"]), str(n["pin"]))
            if "?" not in k[0] and k in where:
                groups.setdefault(where[k], []).append(f"{k[0]}.{k[1]}")
        if len(groups) > 1:
            bad.append({"net": net, "groups": sorted(sorted(g) for g in groups.values())})
    return bad


def _split_scope(ops) -> tuple[set, set] | None:
    """What the batch's delete/disconnect/rename_net/swap ops may split: (refs, net names). None = anything (a
    deleted wire segment or dangling wires can split any net)."""
    refs, names = set(), set()
    for op in ops:
        k = op["op"]
        if k == "delete" and (op.get("wire_at") is not None or op.get("dangling")):
            return None
        if k in ("delete", "swap") and op.get("ref"):
            refs.add(str(op["ref"]))
        if k == "delete" and op.get("label"):
            names.add(str(op["label"]).strip().rsplit("/", 1)[-1])
        if k == "rename_net":
            names |= {str(op.get(x) or "").rsplit("/", 1)[-1] for x in ("from", "to")}
        if k == "disconnect" or (k == "delete" and op.get("pin")):
            try:
                refs |= {str(t).split(".", 1)[0] for t in _pins_of(op)}
            except ToolError:
                return None
    return refs, names


def _merge_fix(ops_used: set) -> str:
    fixes = []
    if "wire" in ops_used:
        fixes.append("join the pins by net name (connect op) instead of a wire")
    if "label" in ops_used:
        fixes.append("anchor the label on a pin (label with 'pin') or pick a point on one wire only")
    if ops_used & {"move", "place", "swap"}:
        fixes.append("give the part another position")
    return "; or ".join(fixes) or "check what the new items touch with sch_read(query='nets')"


def _as_instance(text: str, path: str) -> str:
    """``text`` with each symbol's instance data cut down to the sheet instance ``path``. Exported on its own, a
    sheet used several times reports the references of its first instance; the batch speaks in the references of
    the instance it edits, so the netlist must use those too."""
    tree = sexpr.loads(text)
    for n in sexpr.find_all(tree, "symbol"):
        for proj in sexpr.find_all(sexpr.find(n, "instances") or [], "project"):
            paths = sexpr.find_all(proj, "path")
            if any(str(q[1]) == path for q in paths):
                proj[:] = [e for e in proj if not (isinstance(e, list) and e and e[0] == "path" and str(e[1]) != path)]
    return sexpr.dumps(tree) + "\n"


def _check_merges(ctx: Ctx, pre_text: str | None, ops: list = ()):
    """Refuse the batch when KiCad's netlist of the edited sheet shows two nets joined that the batch did not ask
    to join (a wire run over another net's stub, a label landing on the wrong wire, ...), or one net split in two
    (a moved part leaving a label off its wire) that no delete/disconnect/rename_net/swap op in the batch explains."""
    if ctx.pr is None or not ctx.sch.symbols(include_power=False):
        return
    name = ctx.pr.name or "sheet"
    post_text = ctx.sch.text()
    if getattr(ctx.sch, "other_paths", None):      # a sheet used several times: report the edited instance's refs
        pre_text = pre_text and _as_instance(pre_text, ctx.sch.sheet_path)
        post_text = _as_instance(post_text, ctx.sch.sheet_path)
    try:
        if pre_text:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(2) as ex:      # two kicad-cli runs side by side
                fb = ex.submit(_isolated_netlist, pre_text, name)
                after = _isolated_netlist(post_text, name)
                before = fb.result()
        else:
            before, after = {"nets": {}}, _isolated_netlist(post_text, name)
    except ToolError:
        return                                     # no kicad-cli: the post-write connectivity check still runs
    except Exception as e:                         # a timeout or an unreadable netlist must not abort the batch
        ctx.warnings.append(f"the net merge check did not run ({type(e).__name__}); check sch_read(query='nets')")
        return
    bad = unintended_merges(before, after, ctx.expect + ctx.joins)
    if bad:
        b = bad[0]
        parts = " and ".join("{" + ", ".join(g[:4]) + (" …" if len(g) > 4 else "") + "}" for g in b["groups"][:2])
        raise ToolError(f"this batch would join {parts} on one net ('{b['net']}') without being asked to",
                        code="NET_MERGE", data={"merges": bad[:5]}, fix=_merge_fix({op["op"] for op in ops}))
    scope = _split_scope(ops)
    if scope is None:
        return
    split = [b for b in unintended_splits(before, after)       # splits the batch's delete/disconnect/... may cause
             if b["net"].rsplit("/", 1)[-1] not in scope[1]
             and not any(n["ref"] in scope[0] for n in before["nets"].get(b["net"], []))]
    if split:
        b = split[0]
        parts = " and ".join("{" + ", ".join(g[:4]) + (" …" if len(g) > 4 else "") + "}" for g in b["groups"][:2])
        net = b["net"].rsplit("/", 1)[-1]
        raise ToolError(f"this batch would split net '{net}' into {parts} without being asked to",
                        code="NET_SPLIT", data={"splits": split[:5]},
                        fix=f"add a connect op naming '{net}' for the pins that leave it, or give the part another position")


def apply_ops(pr, ops: list[dict], dry_run=False, while_open="refuse", sheet: str | None = None) -> dict:
    """Apply ops atomically. Returns {'changed','refs','warnings','revision', 'expect', 'region', 'file', ...}."""
    ops = _normalize(ops)
    sheets = {op.get("sheet") for op in ops if op.get("sheet")}
    if len(sheets) > 1:
        raise ToolError("all ops in one sch_apply call must target the same sheet", code="BAD_OP",
                        fix="split the batch per sheet")
    sheet = sheet or (sheets.pop() if sheets else None)
    f, _ = sheet_file(pr, sheet)
    if not dry_run:
        _guard(pr, f, while_open)
    base_rev = P.revision(f) if f.exists() else None
    created = not f.exists()
    sch = load_sheet(pr, sheet)
    switched = _pick_instance(sch, ops)
    ctx = Ctx(pr, sch)
    if switched:
        name = next((h["name"] for h in hierarchy(pr) if h["path"] == switched), switched)
        ctx.changed.append(f"editing sheet instance '{name}' (the references named belong to it)")
    pre_h = {l["net"] for l in sch.labels() if l["kind"] == "hierarchical"}
    joining = any(op["op"] in JOINING_OPS for op in ops)
    pre_text = sch.text() if joining and sch.symbols(include_power=False) else None
    pre_nodes = [n for n in sch.tree if isinstance(n, list)]     # held so their ids stay unique for 'fresh'
    pre_ids = {id(n) for n in pre_nodes}
    try:
        specs = _prepare_places(ctx, ops)
        plan, ext = _planned_nets(ctx, ops, specs)
        if specs:
            _do_places(ctx, specs, plan, ext)
        for i, op in enumerate(ops):
            if op["op"] == "place":
                continue
            try:
                DISPATCH[op["op"]](ctx, op)
            except ToolError as e:
                e.message = f"ops[{i}] ({op['op']}): {e.message}"
                e.data = dict(e.data or {}, op_index=i)
                raise
        if any(op["op"] in ("place", "connect", "power", "label", "move", "swap", "rename_net") for op in ops):
            n_moved = declutter(ctx, fresh=lambda node: id(node) not in pre_ids)
            if n_moved:
                ctx.changed.append(f"moved {n_moved} power symbol(s) further out on their stub to clear neighbouring labels")
        if joining:
            _check_merges(ctx, pre_text, ops)
    except ToolError as e:
        e.data = dict(e.data or {}, applied=False)
        if "nothing was written" not in e.message:
            e.message += " — nothing was written (the batch is atomic)"
        raise
    new_h = sorted({l["net"] for l in sch.labels() if l["kind"] == "hierarchical"} - pre_h - ctx.renamed)
    if new_h:
        is_root = not pr.sch.exists() or f.resolve() == pr.sch.resolve()
        missing = new_h if is_root else [n for n in new_h if n not in _parent_sheet_pins(pr, f)]
        if missing:
            ctx.warnings.append(
                f"hierarchical label(s) {', '.join(missing)} have no matching sheet pin on the parent sheet symbol"
                + (" (this is the root sheet: they connect to nothing)" if is_root else "")
                + " — ERC will flag them. Copper cannot add sheet pins yet: use kind='global' for nets shared between "
                  "sheets, or add the sheet pin in KiCad (Place ▸ Sheet Pin).")
    result = {"file": str(f), "changed": ctx.changed, "refs": ctx.refs, "warnings": ctx.warnings,
              "expect": ctx.expect, "region": _region(ctx.region), "dry_run": dry_run}
    if dry_run:
        result["revision"] = base_rev
        return result
    if f.exists():
        result["checkpoint"] = P.checkpoint(pr, "sch_apply")
    if created:
        from .model import ensure_project_file
        ensure_project_file(pr.pro)
        result["created"] = str(f)
    info = sch.save(f, canonicalize=True, base_revision=base_rev)
    result.update(info)
    result["revision"] = P.revision(f)
    return result


def _region(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


# ── collision avoidance: power symbols vs neighbouring labels ─────────────
DECLUTTER_STEPS = 8      # a power symbol moves outward at most 8 × 2.54 mm


def declutter(ctx: Ctx, fresh=None) -> int:
    """Move power symbols that overlap a neighbouring pin's label (or another power symbol's text) further out
    along their own pin stub until they are clear: the GND triangle on pin 5 no longer sits on 'NRST' of pin 6,
    '+3V3' no longer runs into 'SWDIO'. Only the plain Copper attachment is touched — ONE straight wire from a
    symbol pin to the power symbol's pin with nothing else on either end — and the wire is only LENGTHENED over
    empty sheet (no pin, wire, label, junction, no-connect or symbol body on the way), so connectivity cannot
    change. ``fresh(node) -> bool`` limits the pass to collisions involving something this batch created
    (None = everything, as tidy redraws the whole sheet). Returns the number of symbols moved."""
    sch = ctx.sch

    def key(pt):
        return (round(pt[0] * 100), round(pt[1] * 100))
    wires = sch.wires()
    ends: dict = {}
    for w_ in wires:
        ends.setdefault(key(w_["a"]), []).append(w_)
        ends.setdefault(key(w_["b"]), []).append(w_)
    labels = sch.labels()
    points = {key(l["at"]) for l in labels}                      # anything connectable that is not a wire or a pin
    for kind in ("junction", "no_connect"):
        for n in sch.items(kind):
            a = sexpr.find(n, "at")
            points.add(key((float(a[1]), float(a[2]))))
    syms = sch.symbols()
    pins: dict = {}
    bodies = []                                                   # (node, box) of real parts: bodies + visible fields
    for s in syms:
        for q in s.pins_world():
            pins.setdefault(key(q["xy"]), []).append((s, q))
        if not s.is_power:
            bodies.append((s.node, s.body_bbox()))
            bodies += [(s.node, fb) for fb in s.field_boxes()]
    lboxes = [(l["node"], LAY.label_box(l)) for l in labels]
    power = {id(s.node): (s, [s.body_bbox()] + s.field_boxes()) for s in syms if s.is_power}
    pw_, ph_ = sch.page_size()

    def shrunk(b, by=0.12):
        return [b[0] + by, b[1] + by, b[2] - by, b[3] - by]

    def hits(s, boxes):
        """Nodes whose text/graphics overlap this power symbol's boxes."""
        out = []
        for b in boxes:
            bb = shrunk(b)
            # labels need visible air (0.25 mm): a value text 0.04 mm from the next pin's label reads as one blob
            out += [n for n, lb in lboxes if LAY._rect_hit(b, lb, 0.25)]
            out += [o.node for o, obs in power.values() if o.node is not s.node
                    and any(LAY._rect_hit(bb, ob) for ob in obs)]
        return out

    def path_clear(s, wire, a, b):
        """Nothing connectable, and no drawing, on the segment a→b (a excluded, b included)."""
        for k, lst in pins.items():
            pt = (k[0] / 100, k[1] / 100)
            if any(o.node is not s.node for o, _ in lst) and (_near(pt, b) or _on_segment(pt, a, b)):
                return False
        for k in points:
            pt = (k[0] / 100, k[1] / 100)
            if _near(pt, b) or _on_segment(pt, a, b):
                return False
        box = [min(a[0], b[0]) - 0.05, min(a[1], b[1]) - 0.05, max(a[0], b[0]) + 0.05, max(a[1], b[1]) + 0.05]
        for o in wires:
            if o["node"] is wire["node"]:
                continue
            ob = [min(o["a"][0], o["b"][0]), min(o["a"][1], o["b"][1]), max(o["a"][0], o["b"][0]), max(o["a"][1], o["b"][1])]
            if LAY._rect_hit(box, ob, 0.01):                      # touches, crosses or overlaps another wire
                return False
        if any(_seg_hits_box(a, b, bb, shrink=-0.3) for n, bb in bodies):
            return False
        # nor through another power symbol / PWR_FLAG or its text
        return not any(_seg_hits_box(a, b, ob, shrink=-0.3) for o, obs in power.values() if o.node is not s.node for ob in obs)

    moved = 0
    for s, boxes in sorted(power.values(), key=lambda v: (v[0].at[1], v[0].at[0])):
        if is_flag(s):
            continue
        pw = s.pins_world()
        if len(pw) != 1:
            continue
        q = pw[0]["xy"]
        mine = ends.get(key(q), [])
        if len(mine) != 1 or key(q) in points or len(pins.get(key(q), [])) != 1:
            continue
        wire = mine[0]
        tip = wire["b"] if _near(wire["a"], q) else wire["a"]
        at_tip = pins.get(key(tip), [])
        if not at_tip or any(o.is_power for o, _ in at_tip) or len({id(o.node) for o, _ in at_tip}) != 1:
            continue                                              # not a part's pin, or two parts' pins meet there
        if len(ends.get(key(tip), [])) != 1 or key(tip) in points:
            continue
        out = at_tip[0][1]["out"]
        d = (q[0] - tip[0], q[1] - tip[1])
        if abs(d[0] * out[1] - d[1] * out[0]) > EPS or d[0] * out[0] + d[1] * out[1] <= 0:
            continue                                              # not a straight outward stub
        bad = hits(s, boxes)
        if not bad or (fresh is not None and not fresh(s.node) and not any(fresh(n) for n in bad)):
            continue
        for k in range(1, DECLUTTER_STEPS + 1):
            dx, dy = out[0] * STUB * k, out[1] * STUB * k
            nq = [round(q[0] + dx, 4), round(q[1] + dy, 4)]
            nb = [LAY._shift(b, dx, dy) for b in boxes]
            if any(b[0] < 7 or b[1] < 7 or b[2] > pw_ - 7 or b[3] > ph_ - 7 for b in nb):
                break
            if not path_clear(s, wire, q, nq):
                break                                             # blocked: further out is blocked as well
            if hits(s, nb):
                continue
            if any(LAY._rect_hit(shrunk(b), bb) for b in nb for n, bb in bodies):
                continue
            if any(_seg_hits_box(o["a"], o["b"], b, shrink=0.12) for b in nb for o in wires if o["node"] is not wire["node"]):
                continue
            for xy in sexpr.find_all(sexpr.find(wire["node"], "pts") or [], "xy"):
                if _near((float(xy[1]), float(xy[2])), q):
                    xy[1], xy[2] = num(nq[0]), num(nq[1])
            a = sexpr.find(s.node, "at")
            a[1], a[2] = num(float(a[1]) + dx), num(float(a[2]) + dy)
            _shift_fields(s.node, dx, dy)
            # keep the indexes true for the symbols still to come
            for e in ("a", "b"):
                if _near(wire[e], q):
                    wire[e] = nq
            ends.pop(key(q), None)
            ends.setdefault(key(nq), []).append(wire)
            pins[key(q)] = [x for x in pins.get(key(q), []) if x[0].node is not s.node]
            if not pins[key(q)]:
                del pins[key(q)]
            pins.setdefault(key(nq), []).append((s, dict(pw[0], xy=nq)))
            power[id(s.node)] = (s, nb)
            ctx.region += nb
            moved += 1
            break
    return moved


# ── tidy: re-layout an existing sheet ─────────────────────────────────────
def _partition(nl: dict) -> set:
    parts = set()
    for name, nodes in nl["nets"].items():
        key = frozenset((n["ref"], n["pin"]) for n in nodes)
        if len(key) == 1 and (name.startswith("unconnected-") or name.startswith("Net-(")):
            continue
        parts.add(key)
    return parts


def _isolated_netlist(text: str, name: str) -> dict:
    import tempfile
    from .netlist import export_netlist
    from ..project import Project
    from .model import ensure_project_file
    with tempfile.TemporaryDirectory(prefix="copper-tidy-") as td:
        f = Path(td) / f"{name}.kicad_sch"
        f.write_text(text, encoding="utf-8", newline="\n")
        ensure_project_file(f.with_suffix(".kicad_pro"))
        return export_netlist(Project(Path(td), name), f)


def _label_name(net: str) -> str:
    """KiCad net name → label text ('/USB_DP' → 'USB_DP', 'Net-(D1-A)' → 'D1_A')."""
    m = re.fullmatch(r"Net-\((.+)\)", net)
    if m:
        return re.sub(r"[^A-Za-z0-9_+.\-]", "_", m.group(1).replace("-Pad", "_").replace("-", "_"))
    return net.split("/")[-1] if "/" in net else net


def is_flag(s: SymInst) -> bool:
    """PWR_FLAG (stock or a project copy such as 'pic_programmer:PWR_FLAG')."""
    return s.lib_id.split(":")[-1] == "PWR_FLAG" or (s.ref.startswith("#FLG") and s.value == "PWR_FLAG")


def geo_groups(sch: Schematic) -> list[dict]:
    """Groups of items joined by wires on ONE sheet (no name-based joining):
    [{'pins': [(ref, pin)], 'labels': [label dict], 'power': [SymInst], 'flags': [SymInst]}].
    Used for things KiCad's netlist does not report: PWR_FLAGs and hierarchical/global labels."""
    def key(p):
        return (round(p[0] * 100), round(p[1] * 100))
    parent: dict = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    wires = sch.wires()
    vert: dict = {}
    horiz: dict = {}
    for w in wires:
        ka, kb = key(w["a"]), key(w["b"])
        union(ka, kb)
        if ka[0] == kb[0]:
            vert.setdefault(ka[0], []).append((min(ka[1], kb[1]), max(ka[1], kb[1]), ka))
        elif ka[1] == kb[1]:
            horiz.setdefault(ka[1], []).append((min(ka[0], kb[0]), max(ka[0], kb[0]), ka))

    def t_join(p):
        k = key(p)
        for lo, hi, kw in vert.get(k[0], []):
            if lo < k[1] < hi:
                union(k, kw)
        for lo, hi, kw in horiz.get(k[1], []):
            if lo < k[0] < hi:
                union(k, kw)
    for w in wires:
        t_join(w["a"])
        t_join(w["b"])
    for j in sch.items("junction"):
        a = sexpr.find(j, "at")
        t_join((float(a[1]), float(a[2])))
    labels = sch.labels()
    for l in labels:
        t_join(l["at"])
    groups: dict = {}

    def grp(p):
        return groups.setdefault(find(key(p)), {"pins": [], "labels": [], "power": [], "flags": []})
    for s in sch.symbols():
        pw = s.pins_world()
        if s.is_power:
            for q in pw[:1]:
                grp(q["xy"])["flags" if is_flag(s) else "power"].append(s)
        else:
            for q in pw:
                grp(q["xy"])["pins"].append((s.ref, q["number"]))
    for l in labels:
        grp(l["at"])["labels"].append(l)
    return list(groups.values())


def _ns(kind: str, name: str) -> tuple:
    """Name space a label / power symbol joins by name: globals and power symbols project-wide, local and
    hierarchical labels within the sheet (KiCad joins a local and a hierarchical label of the same name)."""
    return (kind in ("global", "power"), name)


def _group_nets(sch: Schematic, nl: dict) -> list[dict]:
    """geo_groups with g['net'] = KiCad netlist net (by a pin, else by a label / power-symbol name)."""
    pn = {(n["ref"], n["pin"]): net for net, nodes in nl["nets"].items() for n in nodes}
    groups = geo_groups(sch)
    by_name: dict = {}
    for g in groups:
        g["net"] = next((pn[p] for p in g["pins"] if p in pn), None)
        if g["net"]:
            for l in g["labels"]:
                by_name.setdefault(_ns(l["kind"], l["net"]), g["net"])
            for s in g["power"]:
                by_name.setdefault(_ns("power", s.value), g["net"])
    for g in groups:      # label / power-symbol-only groups (e.g. PWR_FLAG + GND symbol) join by name
        if g["net"] is None:
            keys = [_ns(l["kind"], l["net"]) for l in g["labels"]] + [_ns("power", s.value) for s in g["power"]]
            g["net"] = next((by_name[k] for k in keys if k in by_name), None)
    return groups


def _external_signature(sch: Schematic, nl: dict) -> tuple[set, set, list]:
    """What a re-layout must keep besides pin-to-pin connectivity: ({(label, kind, pins of its net)} for
    hierarchical/global labels, {pins of each PWR_FLAG's net}, [items on no pin-bearing net])."""
    pins_of = {net: frozenset((n["ref"], n["pin"]) for n in nodes) for net, nodes in nl["nets"].items()}
    labels, flags, lost = set(), set(), []
    for g in _group_nets(sch, nl):
        sig = pins_of.get(g["net"]) if g["net"] else None
        for l in g["labels"]:
            if l["kind"] != "local":
                if sig is None:
                    lost.append(f"{l['kind']} label {l['net']}")
                else:
                    labels.add((l["net"], l["kind"], sig))
        for s in g["flags"]:
            if sig is None:
                lost.append(f"PWR_FLAG {s.ref}")
            else:
                flags.add(sig)
    return labels, flags, lost


def _net_names_of(sch: Schematic, nl: dict) -> dict:
    """netlist net → {'flags': n, 'ext': {(label, kind, shape)}} for hierarchical/global labels and PWR_FLAGs."""
    out: dict = {}
    for g in _group_nets(sch, nl):
        if g["net"] is None:
            continue
        d = out.setdefault(g["net"], {"flags": 0, "ext": set()})
        d["flags"] += len(g["flags"])
        for l in g["labels"]:
            if l["kind"] != "local":
                d["ext"].add((l["net"], l["kind"], str(sexpr.value(l["node"], "shape", "bidirectional"))))
    return out


def _attach_alias(ctx: Ctx, pins: list, name: str, kind: str, shape: str) -> bool:
    """Second name on a net (e.g. hierarchical label 'VCC_PIC' on a net drawn with a VCC power symbol): a short
    wire branching sideways off one of the net's stubs, ending in the label. False if there is no room."""
    occ = LAY.occupied(ctx.sch)
    for si, p in pins:
        st = ctx.stub_at(p["xy"])
        if not st:
            continue
        q, out = st["q"], p["out"]
        for k in (2, 3, 4, 5):
            for sgn in (1, -1):
                d = (out[1] * sgn, out[0] * sgn)
                q2 = [round(q[0] + d[0] * STUB * k, 4), round(q[1] + d[1] * STUB * k, 4)]
                if not ctx.point_is_free(q2, seg_from=q):
                    continue
                tw = text_width(name) + (3.0 if kind != "local" else 0.6)
                x, y = q2
                box = {(1, 0): [x, y - 1.5, x + tw, y + 1.5], (-1, 0): [x - tw, y - 1.5, x, y + 1.5],
                       (0, -1): [x - 1.5, y - tw, x + 1.5, y], (0, 1): [x - 1.5, y, x + 1.5, y + tw]}[tuple(out)]
                if any(LAY._rect_hit(box, o, 0.0) for o in occ):
                    continue
                ctx.sch.add_wire(q, q2)
                ctx.sch.add_label(name, q2, G.label_angle(out), kind, shape)
                ctx.touch(q, q2)
                return True
    return False


def _shift_fields(node, dx: float, dy: float):
    """Move every field (hidden ones too) with its symbol so nothing is left floating at the old spot."""
    for prop in sexpr.find_all(node, "property"):
        pa = sexpr.find(prop, "at")
        if pa is not None and len(pa) > 2:
            pa[1], pa[2] = num(float(pa[1]) + dx), num(float(pa[2]) + dy)


def tidy(pr, sheet: str | None = None, dry_run: bool = False, while_open: str = "refuse") -> dict:
    """Re-place every symbol with the auto-layout and re-attach all connections as pin-anchored
    labels / power symbols. Connectivity is proven identical (KiCad netlists before/after), and so are the
    things a netlist does not show: hierarchical/global labels (the sheet's interface) and PWR_FLAGs."""
    f, spath = sheet_file(pr, sheet)
    if not f.exists():
        raise ToolError(f"{f.name} does not exist", code="NOT_FOUND")
    if not dry_run:
        _guard(pr, f, while_open)
    base_rev = P.revision(f)
    sch = Schematic.load(f, pr.name, spath)
    if sch.items("sheet"):
        raise ToolError("sch_tidy only re-lays leaf sheets; this sheet contains hierarchical sheet symbols",
                        code="TIDY_UNSUPPORTED", fix="run sch_tidy(sheet='<child>.kicad_sch') on each sub-sheet")
    if sch.items("bus") or sch.items("bus_entry"):
        raise ToolError("sch_tidy does not re-route buses", code="TIDY_UNSUPPORTED")
    _, inst_paths = _hierarchy_context(pr, f)
    if len(inst_paths) > 1:
        raise ToolError(f"{f.name} is used by {len(inst_paths)} sheet instances: tidy would name its auto-named nets "
                        f"after one instance's references, which reads wrong in the others", code="TIDY_UNSUPPORTED",
                        fix="edit this sheet with sch_apply (move / connect / disconnect) instead")
    before = _isolated_netlist(sch.text(), pr.name)
    ext_before = _external_signature(sch, before)
    if ext_before[2]:
        raise ToolError(f"sch_tidy would drop {', '.join(ext_before[2][:5])} (not wired to any pin) — nothing written",
                        code="TIDY_UNSUPPORTED", fix="connect or delete those items first (sch_apply), then tidy")
    names = _net_names_of(sch, before)
    pn = {}
    for net, nodes in before["nets"].items():
        for n in nodes:
            pn[(n["ref"], n["pin"])] = net
    lk: dict[str, set] = {}
    for l in sch.labels():
        lk.setdefault(l["net"], set()).add(l["kind"])

    def label_kind(name):
        ks = lk.get(name) or {"local"}
        return "local" if "local" in ks else "global" if "global" in ks else "hierarchical"
    power_vals = {s.value for s in sch.symbols() if s.is_power and not is_flag(s)}
    ctx = Ctx(pr, sch)
    pnames = ctx.pnames | power_vals

    def style_of(name):
        return "power" if name in power_vals or (name in pnames and label_kind(name) == "local") else "label"
    # hierarchical / global labels and PWR_FLAGs per net (KiCad's netlist does not report them)
    need_kind: dict = {}     # primary name -> [(kind, shape)] given to one of its pin labels (same name)
    aliases: list = []       # (primary name, label, kind, shape) drawn as a branch off one of the net's stubs
    flag_names: list = []
    for net, d in names.items():
        prim = _label_name(net)
        if d["flags"]:
            flag_names.append(prim)
        for name, kind, shape in sorted(d["ext"]):
            if name == prim and style_of(prim) == "label" and kind == label_kind(prim):
                continue
            if name == prim and style_of(prim) == "label" and kind == "hierarchical":
                need_kind.setdefault(prim, []).append((kind, shape))
            else:
                aliases.append((prim, name, kind, shape))
    shapes = {l["net"]: str(sexpr.value(l["node"], "shape", "bidirectional")) for l in sch.labels() if l["kind"] != "local"}
    # an auto-named net may stay label-free only when one chain wire covers it completely (exactly 2 pins);
    # with a 3rd pin the wired pair and the 3rd pin's label would become two different nets
    ctx.unnamed = {_label_name(n) for n, nodes in before["nets"].items()
                   if n.startswith("Net-(") and len({(x["ref"], x["pin"]) for x in nodes}) == 2} - set(flag_names)
    # keep each rail's own power symbol (custom/project power libs, e.g. 'MyLib:VIN'), not a stock look-alike
    for s in sch.symbols():
        if s.is_power and not is_flag(s) and sch.lib_symbol(s.lib_name) is not None \
                and sexpr.find(sch.lib_symbol(s.lib_name), "power") is not None:
            ctx.power_src.setdefault(s.value, (s.lib_id, s.lib_name))
    nc_pins = set()
    for s in sch.symbols(include_power=False):
        for p in s.pins_world():
            if ctx.items_at(p["xy"])["nc"]:
                nc_pins.add((s.ref, p["number"]))
    # decoupling caps stay with the IC they sat next to (the auto-layout would otherwise share them round-robin)
    big = []
    for s in sch.symbols(include_power=False):
        if len({p["number"] for p in s.unit_pins()}) > 4:
            for p in s.pins_world():
                net = pn.get((s.ref, p["number"]))
                if net:
                    big.append((s, p["xy"], net))
    near: dict = {}
    for s in sch.symbols(include_power=False):
        nums = {p["number"] for p in s.unit_pins()}
        if len(nums) != 2:
            continue
        rails = [pn.get((s.ref, n)) for n in sorted(nums)]
        rails = [r for r in rails if r and LAY.is_supply(_label_name(r), pnames)]
        if not rails:
            continue
        cands = [(b, xy) for b, xy, net in big if net == rails[0] and b.ref != s.ref]
        if len({b.ref for b, _ in cands}) < 2:
            continue
        b, _ = min(cands, key=lambda c: (c[1][0] - s.at[0]) ** 2 + (c[1][1] - s.at[1]) ** 2)
        near[f"{s.ref}#{s.unit}"] = f"{b.ref}#{b.unit}"
    for kind in ("wire", "junction", "label", "global_label", "hierarchical_label", "no_connect"):
        for n in list(sch.items(kind)):
            sch.remove(n)
    for s in list(sch.symbols()):
        if s.is_power:
            sch.remove(s.node)
    nodes = []
    items = []
    for s in sch.symbols():
        ls = sch.lib_symbol(s.lib_name)
        if ls is None:
            continue
        lib, _, name = s.lib_id.partition(":")
        lsym = libs.LibSymbol(s.lib_id, lib, name, copy.deepcopy(ls), "")
        nets = {}
        for p in s.unit_pins():
            net = pn.get((s.ref, p["number"]))
            if net and not net.startswith("unconnected-"):
                nets[p["number"]] = _label_name(net)
        two = len({p["number"] for p in s.unit_pins()}) == 2
        it = LAY.Item(key=f"{s.ref}#{s.unit}", libsym=lsym, unit=s.unit, rot=None if two else s.rot,
                      mirror=None if two else s.mirror, value=s.value, ref=s.ref, nets=nets,
                      nc={p["number"] for p in s.unit_pins() if (s.ref, p["number"]) in nc_pins},
                      near=near.get(f"{s.ref}#{s.unit}"))
        items.append(it)
        nodes.append((s, it))
    for s, it in nodes:
        sch.remove(s.node)
    try:
        pos = LAY.arrange(sch, items, pnames, {})
    except RuntimeError as e:
        raise ToolError(str(e), code="NO_ROOM")
    if pos.get("__paper__"):
        sch.set_paper(pos["__paper__"])
    for il in pos.get("__inline__", []):
        ent = dict(il, done=False)
        ctx.inline[tuple(il["a"])] = ent
        ctx.inline[tuple(il["b"])] = ent
    for s, it in nodes:
        a = sexpr.find(s.node, "at")
        p = pos[it.key]
        old = s.at
        a[1:] = [num(p["at"][0]), num(p["at"][1]), int(p["rot"])]
        _shift_fields(s.node, p["at"][0] - old[0], p["at"][1] - old[1])
        for e in list(s.node):
            if isinstance(e, list) and e and e[0] == "mirror":
                s.node.remove(e)
        if it.mirror:
            s.node.insert(s.node.index(a) + 1, [Sym("mirror"), Sym(it.mirror)])
        sch._insert(s.node)
        autoplace_fields(SymInst(sch, s.node))
    ctx.pnames = pnames
    net_pins: dict = {}
    for s, it in nodes:
        si = SymInst(sch, s.node)
        bynet: dict = {}
        for p in si.pins_world():
            if p["number"] in it.nc:
                if not ctx.items_at(p["xy"])["nc"]:
                    sch.add_no_connect(p["xy"])
                continue
            net = it.nets.get(p["number"])
            if not net:
                continue
            net_pins.setdefault(net, []).append((si, p))
            if ctx.net_at_pin(p["xy"], si)[0] == net:
                continue
            bynet.setdefault(net, []).append({"si": si, "xy": p["xy"], "out": p["out"], "pins": [p], "alternate": None,
                                              "label": f"{si.ref}.{p['number']}"})
        for net, gs in bynet.items():
            style = style_of(net)
            if style == "power":
                gs, _ = _power_rails(ctx, gs, net)     # adjacent same-rail pins share one symbol
            for g in gs:
                k, shape = label_kind(net), shapes.get(net, "bidirectional")
                if style == "label" and need_kind.get(net) and not ctx.inline.get((si.ref, g["pins"][0]["number"])):
                    k, shape = need_kind[net].pop()
                _attach(ctx, g, net, style, k if style == "label" else "local", shape)
    for net, lst in need_kind.items():         # every pin of the net was chain-wired: draw them as branches
        for k, shape in lst:
            aliases.append((net, net, k, shape))
    for prim, name, kind, shape in aliases:
        if not _attach_alias(ctx, net_pins.get(prim, []), name, kind, shape):
            raise ToolError(f"tidy found no room to keep {kind} label '{name}' on net {prim} — aborted, nothing written",
                            code="TIDY_CHANGED_NETS", fix="leave this sheet as it is, or move parts apart first")
    declutter(ctx)          # before the flags: they then pick spots around the final power symbols
    for name in dict.fromkeys(flag_names):
        op_pwr_flag(ctx, {"net": name})
    sch.prune_lib_symbols()
    after = _isolated_netlist(sch.text(), pr.name)
    pb, pa = _partition(before), _partition(after)
    if pb != pa:
        lost = [sorted(".".join(x) for x in s_) for s_ in pb - pa][:6]
        raise ToolError("tidy would change connectivity — aborted, nothing written", code="TIDY_CHANGED_NETS",
                        data={"groups_before_not_after": lost})
    ext_after = _external_signature(sch, after)
    if ext_after[:2] != ext_before[:2]:
        gone = [f"{k} label {n}" for n, k, _ in ext_before[0] - ext_after[0]]
        gone += ["a PWR_FLAG"] * len(ext_before[1] - ext_after[1])
        raise ToolError("tidy would drop or move " + (", ".join(gone[:6]) or "sheet interface items")
                        + " — aborted, nothing written", code="TIDY_CHANGED_NETS")
    kept = []
    if ext_before[0]:
        kept.append(f"{len(ext_before[0])} hierarchical/global label(s)")
    if ext_before[1]:
        kept.append(f"{len(ext_before[1])} PWR_FLAG(s)")
    res = {"file": str(f), "moved": len(nodes), "paper": sch.paper,
           "connectivity": "identical (KiCad netlist before/after" + (f"; kept {', '.join(kept)}" if kept else "") + ")",
           "warnings": ctx.warnings, "dry_run": dry_run}
    if dry_run:
        return res
    res["checkpoint"] = P.checkpoint(pr, "sch_tidy")
    res.update(sch.save(f, canonicalize=True, base_revision=base_rev))
    res["revision"] = P.revision(f)
    return res
