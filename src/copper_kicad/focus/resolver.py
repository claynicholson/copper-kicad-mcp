"""Channel C — turn a box or a point into the things it refers to, from the
design files alone. Also the one place focus objects are normalised.

    resolve(pr, document, box=[x0,y0,x1,y1] | near=[x,y], radius=None, sheet=None, side=None) -> dict
    select(pr, document, refs, pins=(), sheet=None) -> dict      # same shape, from explicit references
    to_focus(res, source) -> dict                                # the normalised focus object (no geometry)
    describe(pr, focus) -> dict                                  # re-check a stored focus against today's files

`resolve` result::

    {'document': 'schematic'|'pcb', 'sheet': '/amp' , 'sheet_file': 'amp.kicad_sch',     # schematic only
     'mode': 'box'|'point', 'box_mm': [x0,y0,x1,y1] | 'point_mm': [x,y], 'radius_mm': r,
     'refs': ['C5','R3','U2'],            # natural order; box = every part inside OR touching the box
     'pins': ['U2.EN'],                   # point: the pin/pad under the cursor; box: pins inside the box of
                                          # parts the box only partly covers (the user boxed "these pins")
     'nets': ['/EN','GND'],               # nets of those pins (point on a part body / box: every net inside)
     'items': [{'ref','value','footprint','x','y','rot','bbox','nets':[…],'inside':'full'|'partial',
                'pins':[{'num','name','x','y','net'}] (only the pins in `pins`), 'side'|'unit'}],
     'nearest': {'ref','dist_mm'},        # point that hit nothing: closest part, so the model can still orient
     'parts_within_mm': [x0,y0,x1,y1],    # …and, when the point is outside every part, the box that holds them
     'also': ['C37 (bottom)'],            # point: other parts under the same spot (other side / overlapping)
     'tracks': n, 'sheets': ['amp'],      # pcb: track segments in the box; schematic: sheet symbols hit
     'stamp': '…'}                        # change token of the files this was resolved against
"""
from __future__ import annotations

import math
import time

from ..mcp.registry import ToolError
from . import geometry as G
from . import scene as S

DEFAULT_RADIUS = {"schematic": 5.08, "pcb": 3.0}
PIN_TOL_SCH = 1.0          # mm from a pin's line (pins sit on a 2.54 mm pitch)
PAD_GROW = 0.08            # mm a click may miss a pad's edge by
TRACK_TOL = 0.15           # mm beyond a track's half width
NET_CAP = 40


# ── helpers ───────────────────────────────────────────────────────────────
def pin_label(item: dict, pin: dict) -> str:
    """'U2.EN' when the name identifies the pin on this part, else 'U2.7'."""
    name = pin.get("name") or ""
    if name and sum(1 for p in item["pins"] if p.get("name") == name) == 1:
        return f"{item['ref']}.{name}"
    return f"{item['ref']}.{pin['num']}"


def _nets_of(pins) -> list[str]:
    out, seen = [], set()
    for p in pins:
        n = p.get("net") or ""
        if n and n not in seen and not S.is_unconnected(n):
            seen.add(n)
            out.append(n)
    return out


def _pad_tol(item: dict, pin: dict) -> float:
    """How close to a pad centre a click must be to mean the pad: just under half the pitch."""
    best = None
    for q in item["pins"]:
        if q is pin or (q["x"] == pin["x"] and q["y"] == pin["y"]):
            continue
        d = math.hypot(q["x"] - pin["x"], q["y"] - pin["y"])
        best = d if best is None else min(best, d)
    return 1.0 if best is None else max(0.3, min(1.5, 0.45 * best))


def _median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0.0


def pad_hit(p: dict, pt, grow: float = PAD_GROW) -> bool:
    """Is board point `pt` on pad `p` (its rectangle, in the pad's own frame when it is rotated by 'a')?
    canvas.js `padHit` is a port."""
    dx, dy = pt[0] - p["x"], pt[1] - p["y"]
    if p.get("a"):
        a = math.radians(p["a"])
        dx, dy = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
    return abs(dx) <= p["hw"] + grow and abs(dy) <= p["hh"] + grow


def pick_pin(document: str, item: dict, pt) -> dict | None:
    """The pin/pad of `item` under point `pt`, or None when the point means the part as a whole.
    Schematic: within 1 mm of the pin's line (on ICs also its name). Board: inside the pad (or, when pad
    sizes are unknown, within just under half the pitch of its centre). canvas.js `pickPin` is a port."""
    best, bd = None, 1e9
    for p in item["pins"]:
        if document == "schematic":
            d = G.dist_point_seg(pt, (p["x"], p["y"]), (p.get("ix", p["x"]), p.get("iy", p["y"])))
            ok = d <= PIN_TOL_SCH
        else:
            d = math.hypot(pt[0] - p["x"], pt[1] - p["y"])
            if p.get("hw"):
                ok = pad_hit(p, pt)
            else:
                ok = d <= _pad_tol(item, p)
        if ok and d < bd:
            best, bd = p, d
    if best is None:
        return None
    c = G.box_center(item["bbox"])
    if document == "schematic" and len(item["pins"]) <= 4:
        # small parts (R, C, D, Q): only the pin line itself — clicking the body means the part
        if math.hypot(pt[0] - c[0], pt[1] - c[1]) <= bd:
            return None
    if document == "pcb" and is_body_pad(item, best):
        return None     # exposed / thermal pad under a package: clicking there means the part
    return best


def is_body_pad(item: dict, pad: dict) -> bool:
    """A pad in the INTERIOR of the pad field (other-numbered pads on all four sides) that is either much
    larger than the rest (exposed pad) or one of several pads sharing a number (thermal pad array).
    BGA balls (unique numbers, equal size) and header pins (not interior) stay pickable."""
    e = 0.05
    others = [q for q in item["pins"] if q["num"] != pad["num"]]
    if not (any(q["x"] < pad["x"] - e for q in others) and any(q["x"] > pad["x"] + e for q in others)
            and any(q["y"] < pad["y"] - e for q in others) and any(q["y"] > pad["y"] + e for q in others)):
        return False
    if sum(1 for q in item["pins"] if q["num"] == pad["num"]) > 1:
        return True
    area = (pad.get("hw") or 0) * (pad.get("hh") or 0)
    return area > 0 and area > 2 * _median([(q.get("hw") or 0) * (q.get("hh") or 0) for q in item["pins"]])


def pick_item(document: str, items: list, pt, radius: float, side: str | None = None, prefer: str | None = None):
    """Nearest item to a point: (item, dist). Inside a box counts as distance 0; ties go to the `prefer`
    board side (the side being looked at), then to the part that has a PAD under the point (a connector pad
    under the courtyard of a part on the other side, or of a 45° neighbour, still means the connector), then
    to the smaller part (a capacitor inside a module's courtyard), then to the nearer centre.
    canvas.js `pickItem` is a port of this."""
    best, key = None, None
    for it in items:
        if side and it.get("side") and it["side"] != side:
            continue
        d = G.dist_point_box(pt, it["bbox"])
        if d > radius:
            continue
        c = G.box_center(it["bbox"])
        on_pad = d == 0 and document == "pcb" and any(p.get("hw") and pad_hit(p, pt) for p in it["pins"])
        k = (round(d, 4), 1 if (prefer and it.get("side") and it["side"] != prefer) else 0, 0 if on_pad else 1,
             G.box_area(it["bbox"]) if d == 0 else 0.0, math.hypot(pt[0] - c[0], pt[1] - c[1]))
        if key is None or k < key:
            best, key = it, k
    return best, (key[0] if key else None)


def _item_out(it: dict, pins=None, inside: str | None = None) -> dict:
    d = {k: it[k] for k in ("ref", "value", "footprint", "x", "y", "rot", "bbox") if k in it}
    for k in ("side", "unit", "dnp"):
        if k in it:
            d[k] = it[k]
    d["nets"] = _nets_of(it["pins"])
    if inside:
        d["inside"] = inside
    if pins:
        d["pins"] = [{k: p[k] for k in ("num", "name", "x", "y", "net") if p.get(k) not in (None, "")} for p in pins]
    return d


def _base(pr, sc: S.Scene) -> dict:
    out = {"document": sc.document}
    if sc.sheet:
        out["sheet"] = sc.sheet["name"]
        out["sheet_file"] = sc.sheet["file"].name
    return out


def _finish(out: dict, sc: S.Scene, nets: list[str]) -> dict:
    out["refs"] = sorted({i["ref"] for i in out["items"]}, key=G.natural_key)
    uniq = list(dict.fromkeys(nets))
    out["nets"] = uniq[:NET_CAP]
    if len(uniq) > NET_CAP:
        out["nets_total"] = len(uniq)
    out["stamp"] = sc.stamp
    if sc.warnings:
        out["warnings"] = list(sc.warnings)
    return out


def _label_nets(sc: S.Scene, names, known) -> list[str]:
    """Label text → the netlist's name for it ('EN' → '/EN') when unambiguous."""
    out = []
    for n in names:
        if n in known:
            out.append(n)
            continue
        c = [k for k in known if k.rsplit("/", 1)[-1] == n]
        out.append(c[0] if len(c) == 1 else n)
    return out


# ── resolve ───────────────────────────────────────────────────────────────
def _check_xy(v, n, name):
    if not isinstance(v, (list, tuple)) or len(v) != n:
        raise ToolError(f"{name} must be {'[x0, y0, x1, y1]' if n == 4 else '[x, y]'} in mm, got {v!r}",
                        code="BAD_ARGUMENT",
                        fix="focus(action='resolve', document='pcb', box=[100,100,120,115]) or near=[112.5,108]")
    try:
        out = [float(x) for x in v]
    except (TypeError, ValueError):
        raise ToolError(f"{name} must contain numbers (mm), got {v!r}", code="BAD_ARGUMENT")
    if not all(math.isfinite(x) for x in out):
        raise ToolError(f"{name} must contain finite numbers (mm), got {v!r}", code="BAD_ARGUMENT")
    return out


def resolve(pr, document: str, box=None, near=None, radius: float | None = None, sheet=None,
            side: str | None = None, scene: S.Scene | None = None, prefer_side: str | None = None) -> dict:
    """What is inside/touching `box` or nearest to `near` (mm, document coordinates). See module docstring.
    side = only footprints on that board side; prefer_side = tie-break only (point mode)."""
    if (box is None) == (near is None):
        raise ToolError("pass exactly one of box=[x0,y0,x1,y1] or near=[x,y] (mm)", code="BAD_ARGUMENT",
                        fix="focus(action='resolve', document='schematic', near=[120.65, 80.01])")
    if side not in (None, "top", "bottom"):
        raise ToolError(f"side must be 'top' or 'bottom', got {side!r}", code="BAD_ARGUMENT")
    sc = scene or S.load(pr, document, sheet)
    out = _base(pr, sc)
    known = {p["net"] for it in sc.items for p in it["pins"] if p.get("net")}
    items = [i for i in sc.items if not (side and i.get("side") and i["side"] != side)]

    if box is not None:
        b = G.norm_box(_check_xy(box, 4, "box"))
        out.update(mode="box", box_mm=G.rbox(b), items=[], pins=[])
        nets: list[str] = []
        for it in items:
            if not G.box_intersects(b, it["bbox"]):
                continue
            full = G.box_contains(b, it["bbox"])
            inside = [p for p in it["pins"] if G.point_in_box((p["x"], p["y"]), b)]
            if full:
                out["items"].append(_item_out(it, inside="full"))
                nets += _nets_of(it["pins"])
            else:
                out["items"].append(_item_out(it, pins=inside, inside="partial"))
                out["pins"] += [pin_label(it, p) for p in inside]
                nets += _nets_of(inside) if inside else []
        if sc.document == "schematic":
            nets += _label_nets(sc, [lb["net"] for lb in sc.labels if G.point_in_box((lb["x"], lb["y"]), b)], known)
            hit = [s["name"] for s in sc.subsheets if G.box_intersects(b, s["bbox"])]
            if hit:
                out["sheets"] = hit
        else:
            n = 0
            for t in sc.tracks:
                if ("at" in t and G.point_in_box(t["at"], b)) or ("a" in t and G.seg_intersects_box(t["a"], t["b"], b)):
                    n += 1
                    if t["net"] and not S.is_unconnected(t["net"]):
                        nets.append(t["net"])
            if n:
                out["tracks"] = n
        if not out["pins"]:
            del out["pins"]
        return _finish(out, sc, nets)

    pt = _check_xy(near, 2, "near")
    try:
        r = DEFAULT_RADIUS[sc.document] if radius is None else float(radius)
    except (TypeError, ValueError):
        raise ToolError(f"radius must be a number of mm, got {radius!r}", code="BAD_ARGUMENT")
    if not (0 <= r <= 500):
        raise ToolError(f"radius must be 0–500 mm, got {radius!r}", code="BAD_ARGUMENT")
    out.update(mode="point", point_mm=G.rbox(pt), radius_mm=G.r3(r), items=[])
    it, dist = pick_item(sc.document, items, pt, r, prefer=prefer_side)
    nets = []
    track = None
    if sc.document == "pcb" and (it is None or dist > 0):
        for t in sc.tracks:
            d = (math.hypot(pt[0] - t["at"][0], pt[1] - t["at"][1]) if "at" in t
                 else G.dist_point_seg(pt, t["a"], t["b"]))
            if d <= t.get("w", 0.2) / 2 + TRACK_TOL:
                track = t
                break
    if track is not None:
        # the cursor is on copper outside any part: the user means the net
        out["tracks"] = 1
        if track["net"]:
            nets.append(track["net"])
        if track.get("layer"):
            out["layer"] = track["layer"]
    elif it is not None:
        pin = pick_pin(sc.document, it, pt)
        if pin is not None:
            out["items"].append(_item_out(it, pins=[pin]))
            out["pins"] = [pin_label(it, pin)]
            nets = _nets_of([pin])
        else:
            out["items"].append(_item_out(it))
            nets = _nets_of(it["pins"])
        if dist:
            out["dist_mm"] = G.r3(dist)
        else:
            # other parts under the same point (other board side, overlapping courtyards): say so, so a model
            # can disambiguate with side= instead of trusting a coin toss
            also = [o["ref"] + (f" ({o['side']})" if o.get("side") else "") for o in items
                    if o is not it and o["ref"] != it["ref"] and G.point_in_box(pt, o["bbox"])]
            if also:
                out["also"] = also[:6]
    else:
        if sc.document == "schematic":
            hit = [s["name"] for s in sc.subsheets if G.point_in_box(pt, s["bbox"])]
            if hit:
                out["sheets"] = hit
            lb = min(sc.labels, key=lambda l: math.hypot(pt[0] - l["x"], pt[1] - l["y"]), default=None)
            if lb and math.hypot(pt[0] - lb["x"], pt[1] - lb["y"]) <= 2.54:
                nets = _label_nets(sc, [lb["net"]], known)
        far, fd = pick_item(sc.document, items, pt, 1e9)
        if far is not None:
            out["nearest"] = {"ref": far["ref"], "dist_mm": G.r3(fd)}
        used = G.union([i["bbox"] for i in items])
        if used and not G.point_in_box(pt, used):
            out["parts_within_mm"] = G.rbox(used)     # the point is outside everything: where the parts are
    return _finish(out, sc, nets)


def select(pr, document: str, refs, pins=(), sheet=None, scene: S.Scene | None = None) -> dict:
    """Same result shape as `resolve`, from explicit references (a KiCad selection, an accumulated
    shift-click selection, or refs named in chat). Unknown refs come back in 'missing'."""
    sc = scene or S.load(pr, document, sheet)
    out = _base(pr, sc)
    by = sc.by_ref()
    out.update(mode="selection", items=[])
    want_pins: dict = {}
    for p in pins or ():
        ref, _, pn = str(p).partition(".")
        want_pins.setdefault(ref, []).append(pn)
    nets, missing, plabels = [], [], []
    for ref in dict.fromkeys(list(refs or []) + list(want_pins)):
        its = by.get(ref)
        if not its:
            missing.append(ref)
            continue
        for it in its:
            sel = [p for p in it["pins"] if p["num"] in want_pins.get(ref, ()) or
                   (p.get("name") and p["name"] in want_pins.get(ref, ()))]
            out["items"].append(_item_out(it, pins=sel))
            if sel:
                plabels += [pin_label(it, p) for p in sel]
            nets += _nets_of(sel) if sel else _nets_of(it["pins"])
    if plabels:
        out["pins"] = list(dict.fromkeys(plabels))
    bb = G.union([i["bbox"] for i in out["items"]])
    if bb:
        out["box_mm"] = G.rbox(bb)
    if missing:
        out["missing"] = missing
    return _finish(out, sc, nets)


# ── the normalised focus object ───────────────────────────────────────────
FOCUS_KEYS = ("document", "sheet", "sheet_file", "mode", "box_mm", "point_mm", "refs", "pins", "nets",
              "nets_total", "sheets", "tracks", "layer", "nearest", "also", "parts_within_mm")


def to_focus(res: dict, source: str, ts: float | None = None) -> dict:
    """Strip a resolve/select result to the stored focus object:
    {document, sheet?, mode, box_mm?|point_mm?, refs, nets, pins?, source, ts, stamp} (+ age_s when read back)."""
    f = {k: res[k] for k in FOCUS_KEYS if k in res and res[k] not in (None, [], "")}
    f.setdefault("refs", [])
    f.setdefault("nets", [])
    f["source"] = source
    f["ts"] = round(time.time() if ts is None else ts, 3)
    f["stamp"] = res.get("stamp", "")
    return f


def chip(focus: dict | None) -> str:
    """What the canvas shows the user: 'R3, C5, U2.EN'."""
    if not focus:
        return ""
    pins = [] if focus.get("mode") == "box" else list(focus.get("pins") or [])   # a box names parts
    pin_refs = {p.split(".", 1)[0] for p in pins}
    parts = [r for r in focus.get("refs", []) if r not in pin_refs] + pins
    parts.sort(key=G.natural_key)
    if not parts:
        if focus.get("sheets"):
            return "sheet " + ", ".join(focus["sheets"])
        if focus.get("nets"):
            return "net " + ", ".join(focus["nets"][:3])
        if focus.get("box_mm"):
            b = focus["box_mm"]
            return f"empty area {b[2] - b[0]:.1f} × {b[3] - b[1]:.1f} mm"
        if focus.get("point_mm"):
            return "point ({:.2f}, {:.2f})".format(*focus["point_mm"])
        return ""
    return ", ".join(parts[:12]) + (f" +{len(parts) - 12}" if len(parts) > 12 else "")


def describe(pr, focus: dict) -> dict:
    """Re-check a stored focus against the files as they are NOW:
    {'items': [...], 'missing': [refs no longer in the design], 'stale': design changed since pointing,
     'region': [x0,y0,x1,y1] worth rendering, 'scene': Scene}."""
    sc = S.load(pr, focus["document"], focus.get("sheet"))
    res = select(pr, focus["document"], focus.get("refs", []), focus.get("pins", ()), scene=sc)
    boxes = [i["bbox"] for i in res["items"]]
    if focus.get("box_mm"):
        if focus.get("mode") == "box":      # the user drew this region: show it, not every big part it grazes
            boxes = []
        boxes.append(focus["box_mm"])
    if focus.get("point_mm"):
        x, y = focus["point_mm"]
        boxes.append([x, y, x, y])
    return {"items": res["items"], "missing": res.get("missing", []), "nets": res["nets"],
            "stale": bool(focus.get("stamp")) and focus.get("stamp") != sc.stamp,
            "region": G.union(boxes), "scene": sc}
