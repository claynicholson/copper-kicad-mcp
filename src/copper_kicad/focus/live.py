"""Channel B — the selection in the running KiCad PCB editor as a focus.

Optional: needs KiCad's IPC API (Preferences ▸ Plugins ▸ Enable KiCad API) and
the ``kicad-python`` package (``kipy``). A box-drag in KiCad *is* the bounding
box: whatever it selected (footprints, pads, tracks, vias, zones) becomes
refs / pins / nets, and the union of their positions becomes ``box_mm``.

KiCad 9/10 expose **no schematic IPC**: a schematic selection cannot be read —
use the Copper Canvas for schematics.

    available() -> {'available': bool, 'hint': str | None, ...}
    selection(pr) -> focus dict | None      (None = nothing selected)   raises ToolError when the API is off
"""
from __future__ import annotations

import hashlib

from ..mcp.registry import ToolError
from . import geometry as G
from . import resolver as R
from . import scene as S
from . import store

FIX = ("1) KiCad ▸ Preferences ▸ Plugins ▸ tick 'Enable KiCad API' (or run `copper kicad-api enable` while KiCad is "
       "closed) and open the board in the PCB editor; 2) install the optional package into Copper's Python: "
       "`pip install kicad-python`. Without it use the Copper Canvas: focus(action='open').")
NO_SCH = "KiCad 9/10 have no schematic IPC API — a schematic selection cannot be read; point on the Copper Canvas instead"


def available() -> dict:
    from ..pcb import live as pcblive
    cap = dict(pcblive.capability())
    cap["schematic"] = False
    cap["schematic_note"] = NO_SCH
    return cap


def _mm(v) -> float:
    return float(v) / 1e6


def _xy(obj, *names):
    for n in names:
        p = getattr(obj, n, None)
        if p is not None and hasattr(p, "x"):
            return [_mm(p.x), _mm(p.y)]
    return None


def _net(obj) -> str:
    n = getattr(obj, "net", None)
    return str(getattr(n, "name", "") or "") if n is not None else ""


def _ref(fp) -> str:
    try:
        return str(fp.reference_field.text.value)
    except Exception:  # noqa: BLE001
        return ""


def read_selection(board) -> dict:
    """Raw selection of a kipy Board: {'refs', 'pads': [(x, y, number, net)], 'nets', 'points', 'kinds'}."""
    refs, pads, nets, pts, kinds = [], [], [], [], {}
    for it in board.get_selection():
        kind = type(it).__name__
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "FootprintInstance":
            r = _ref(it)
            if r:
                refs.append(r)
            p = _xy(it, "position")
            if p:
                pts.append(p)
        elif kind == "Pad":
            p = _xy(it, "position")
            if p:
                pads.append((p[0], p[1], str(getattr(it, "number", "") or ""), _net(it)))
                pts.append(p)
            if _net(it):
                nets.append(_net(it))
        elif kind in ("Track", "ArcTrack"):
            for p in (_xy(it, "start"), _xy(it, "end")):
                if p:
                    pts.append(p)
            if _net(it):
                nets.append(_net(it))
        elif kind == "Via":
            p = _xy(it, "position")
            if p:
                pts.append(p)
            if _net(it):
                nets.append(_net(it))
        else:
            if _net(it):
                nets.append(_net(it))
            try:
                bb = board.get_item_bounding_box(it)
                if bb is not None:
                    pts.append([_mm(bb.pos.x), _mm(bb.pos.y)])
                    pts.append([_mm(bb.pos.x + bb.size.x), _mm(bb.pos.y + bb.size.y)])
            except Exception:  # noqa: BLE001
                pass
    return {"refs": list(dict.fromkeys(refs)), "pads": pads, "nets": list(dict.fromkeys(nets)),
            "points": pts, "kinds": kinds}


def focus_from_selection(pr, sel: dict, board_name: str | None = None) -> dict | None:
    """Normalise a raw selection against the board FILE (values, footprints and nets come from the saved board)."""
    if not (sel["refs"] or sel["pads"] or sel["nets"] or sel["points"]):
        return None
    sc = S.load(pr, "pcb")
    pins = []
    for x, y, num, _net_name in sel["pads"]:      # a pad knows its number but not its parent: match by position
        best, bd = None, 0.05
        for it in sc.items:
            for p in it["pins"]:
                d = abs(p["x"] - x) + abs(p["y"] - y)
                if d <= bd and (not num or p["num"] == num):
                    best, bd = f"{it['ref']}.{p['num']}", d
        if best:
            pins.append(best)
    res = R.select(pr, "pcb", sel["refs"], pins, scene=sc)
    nets = list(dict.fromkeys(res["nets"] + [n for n in sel["nets"] if not S.is_unconnected(n)]))
    res["nets"] = nets[:R.NET_CAP]
    boxes = [i["bbox"] for i in res["items"]] + [[p[0], p[1], p[0], p[1]] for p in sel["points"]]
    bb = G.union(boxes)
    if bb:
        res["box_mm"] = G.rbox(bb)
    sig = hashlib.sha1(repr((sel["refs"], pins, sel["nets"], res.get("box_mm"))).encode()).hexdigest()[:12]
    f = R.to_focus(res, "kicad", ts=store.kicad_mark(pr, sig))
    f["selected"] = sel["kinds"]
    if res.get("missing"):
        f["missing"] = res["missing"]
    if board_name and board_name.lower() != pr.pcb.name.lower():
        f["warning"] = (f"KiCad has '{board_name}' open but the active Copper project is '{pr.pcb.name}' — "
                        f"project_open(path=…) the project that is open in KiCad")
    return f


def selection(pr) -> dict | None:
    cap = available()
    if not cap["available"]:
        raise ToolError(f"KiCad live selection is not available: {cap['hint']}", code="NO_LIVE_KICAD", fix=FIX,
                        data={"capability": {k: cap[k] for k in ("kipy", "socket", "available")}})
    try:
        from kipy import KiCad  # type: ignore
        board = KiCad(timeout_ms=4000).get_board()
        sel = read_selection(board)
        name = str(getattr(board, "name", "") or "") or None
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"could not read the KiCad selection: {type(e).__name__}: {e}", code="NO_LIVE_KICAD",
                        fix="open the board in KiCad's PCB editor (the API only answers while it is open), "
                            "or point on the Copper Canvas: focus(action='open')")
    return focus_from_selection(pr, sel, name)
