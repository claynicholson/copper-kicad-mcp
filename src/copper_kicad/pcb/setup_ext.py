"""Board setup helpers behind pcb_setup / pcb_place / pcb_route: outline import, automatic board
size, track polish. Server side: no pcbnew here, every board edit goes through ``board.run_ops``.

    prepare_outline(pr, ...)     parse a CAD file -> worker ops + description + findings (writes nothing)
    after_outline(pr, imp, ...)  findings that need the written board (parts / copper now outside)
    size_ops(pr, snap, ...)      auto-size -> worker ops for the rectangle (+ corner holes)
    polish(pr, before_uuids, ...)  clean up the tracks a route call created, revert on any regression
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import project as projmod
from ..cad import ir, outline as cadout
from ..mcp.registry import Image, ToolError
from . import board, elegant


def _tool_error(e: cadout.OutlineError) -> ToolError:
    return ToolError(e.message, code=e.code, fix=e.fix, data=e.data)


class Imported:
    def __init__(self, ol, rep, ops, findings):
        self.ol, self.rep, self.ops, self.findings = ol, rep, ops, findings

    def describe(self, candidates=None):
        d = cadout.describe(self.ol, self.rep, candidates=candidates)
        w = getattr(self, "written", None)
        if w:       # what pcbnew actually wrote (after the clearance offset)
            d["size"], d["area_mm2"], d["shapes"] = w["size"], w["area"], w["shapes"]
        return d

    def preview(self, pr, show_candidates=False) -> list:
        try:
            if show_candidates:
                self.rep["show_candidates"] = True
            out = Path(pr.out_dir) / "outline-pick.png"
            cadout.preview(self.ol, self.rep, out)
            return [Image(path=str(out), caption=f"outline picked from {self.rep['file']}")]
        except Exception:  # noqa: BLE001 — the picture is a courtesy; never fail the import over it
            return []


def board_thickness(pr):
    try:
        m = re.search(r"\(general\s*\(thickness\s+([\d.]+)\)", pr.pcb.read_text(encoding="utf-8", errors="replace")[:20000])
        return float(m.group(1)) if m else None
    except OSError:
        return None


def prepare_outline(pr, outline_file, mode="auto", pick=None, axis=None, layer=None, units=None, clearance=None,
                    flip=False, holes_policy="mounting", keep_position=False, origin=None, holes=None, hole_net=None,
                    snap0=None) -> Imported:
    """Read the CAD file and build the worker ops. Raises ToolError; never writes."""
    try:
        path = cadout.find_file(outline_file, pr.dir)
        org = origin or ((snap0 or {}).get("outline") or {}).get("bbox", [100.0, 100.0])[:2]
        ol, rep = cadout.load(path, mode=mode, pick=pick, axis=axis, layer=layer, units=units, clearance=clearance,
                              flip=bool(flip), holes="none" if holes == "none" else holes_policy,
                              keep_position=bool(keep_position), origin=(float(org[0]), float(org[1])))
    except cadout.OutlineError as e:
        raise _tool_error(e)
    if holes and holes != "none":           # the caller fixed the hole size; detected positions stay
        for h in ol["holes"]:
            h["size"], h["d"] = holes, ir.MOUNT_D[holes]
    ops = [cadout.to_shapes_op(ol, rep)]
    groups = {}
    for h in ol["holes"]:
        if h.get("size"):
            groups.setdefault(h["size"], []).append([h["x"], h["y"]])
    try:
        reserved = [c["ref"] for c in board.netlist(pr)["components"]] if pr.sch.exists() else []
    except ToolError:
        reserved = []
    ops.append({"op": "holes_multi", "groups": [{"size": s, "positions": p} for s, p in sorted(groups.items())],
                "lib_path": board.fp_libs(pr).get("MountingHole"), "reserved_refs": reserved,
                "net": hole_net, "plated": bool(hole_net)})
    f = cadout.findings(ol, rep)
    t_board = board_thickness(pr)
    if ol.get("thickness") and t_board and abs(ol["thickness"] - t_board) > 0.05:
        f.append({"severity": "info", "code": "BOARD_THICKNESS_DIFFERS",
                  "message": f"the file's plate is {ol['thickness']} mm thick, the board is set to {t_board:g} mm "
                             "(the thickness is not changed by an outline import)",
                  "fix": "set it in KiCad: Board Setup > Physical Stackup"})
    return Imported(ol, rep, ops, f)


def _pip(poly, x, y):
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            c = not c
    return c


def after_outline(pr, snap0, checkpoint=None) -> list:
    """OUTLINE_REPLACES_ROUTED when a board that already had copper or locked parts got a new outline."""
    had = (snap0.get("tracks") or 0) + (snap0.get("vias") or 0)
    zones = [z for z in snap0.get("zones") or [] if not z.get("rule_area")]
    if not had and not zones:
        return []
    snap = board.read(pr, what=["footprints", "tracks"])
    ol = snap.get("outline") or {}
    poly = ol.get("polygon")
    if not poly:
        return []
    holes = ol.get("holes") or []

    def inside(x, y):
        return _pip(poly, x, y) and not any(_pip(h, x, y) for h in holes)
    t_out = v_out = 0
    for t in snap.get("track_list") or []:
        if t["type"] == "via":
            v_out += not inside(*t["at"])
        else:
            t_out += not (inside(*t["start"]) and inside(*t["end"]))
    fp_out = [f["ref"] for f in snap.get("footprints") or [] if "board_only" not in (f.get("flags") or []) and not inside(f["x"], f["y"])]
    return [{"severity": "warning", "code": "OUTLINE_REPLACES_ROUTED",
             "message": f"the outline of a routed board was replaced ({had} tracks/vias, {len(zones)} zones kept as they were): "
                        f"{t_out} tracks, {v_out} vias and {len(fp_out)} footprints now lie outside the new outline"
                        + (f" ({', '.join(fp_out[:8])})" if fp_out else ""),
             "fix": (f"checkpoint(action='restore', id='{checkpoint}') undoes it; " if checkpoint else "")
                    + "otherwise move / re-route what is outside, then pcb_pour(fill_only=true)"}]


def size_ops(pr, snap, holes=None, hole_inset=3.5, origin=None, width=None, height=None, radius=0.0, hole_net=None):
    """Auto-size the board for the parts in ``snap`` (a geom snapshot).
    -> (ops, info) or (None, info) when nothing fits (info['failed'])."""
    has_holes = any(elegant._is_hole(d) and "board_only" in (d.get("flags") or []) for d in snap["footprints"])
    want = holes if holes and holes != "none" else None
    # the schematic has its own mounting holes: they take the corner seats (pcb_place moves them there),
    # so no board-only holes are added on top
    from . import holes as holesmod
    sch_holes = [d["ref"] for d in holesmod.schematic_holes(snap["footprints"])]
    if sch_holes:
        want = None
    new_d = ir.MOUNT_D.get(want) if (want and not has_holes) else None
    snap = dict(snap, track_segs=[])
    info = elegant.auto_size(snap, hole_inset=hole_inset, origin=origin, width=width, height=height, new_holes=new_d)
    if sch_holes:
        info["schematic_holes"] = sch_holes
    if info.get("failed"):
        return None, info
    w, h = info["width"], info["height"]
    ox, oy = info["origin"]
    ops = [{"op": "outline", "width": w, "height": h, "radius": radius if radius else min(1.5, w / 8, h / 8), "origin": [ox, oy]},
           {"op": "outline_prop", "set": {"src": "auto"}, "unset": ["top", "bottom", "file", "mode", "placed"]}]
    if want or has_holes:
        size = want
        if not size:
            vals = [d.get("value") for d in snap["footprints"] if elegant._is_hole(d) and "board_only" in (d.get("flags") or [])]
            size = next((v for v in vals if v in ir.MOUNT_D), "M3")
        try:
            reserved = [c["ref"] for c in board.netlist(pr)["components"]] if pr.sch.exists() else []
        except ToolError:
            reserved = []
        ops.append({"op": "holes_multi", "groups": [{"size": size, "positions": elegant.corner_holes(ox, oy, w, h, hole_inset)}],
                    "lib_path": board.fp_libs(pr).get("MountingHole"), "reserved_refs": reserved,
                    "net": hole_net, "plated": bool(hole_net)})
    return ops, info


def auto_size_refusal(snap) -> str | None:
    """Why auto-size must not run on this board (None = fine)."""
    n = (snap.get("tracks") or 0) + (snap.get("vias") or 0)
    if n:
        return f"the board has {snap.get('tracks')} tracks and {snap.get('vias')} vias"
    zones = [z for z in snap.get("zones") or [] if not z.get("rule_area")]
    if zones:
        return f"the board has {len(zones)} copper zones"
    locked = [f["ref"] for f in snap.get("footprints") or [] if f.get("locked") and "board_only" not in (f.get("flags") or [])]
    if locked:
        return f"{len(locked)} footprints are locked ({', '.join(locked[:6])})"
    return None


def edge_rules(pr) -> dict:
    """Copper-to-edge and hole clearance of the project (mm), 0.3 when unknown."""
    out = {"copper_edge": 0.3, "hole_clearance": 0.3}
    try:
        import json
        r = json.loads(pr.pro.read_text(encoding="utf-8"))["board"]["design_settings"]["rules"]
        if r.get("min_copper_edge_clearance"):
            out["copper_edge"] = float(r["min_copper_edge_clearance"])
        out["hole_clearance"] = max(float(r.get("min_hole_clearance") or 0.0), out["copper_edge"])
    except Exception:  # noqa: BLE001
        pass
    return out


# ── polish after routing ──────────────────────────────────────────────────
def track_uuids(pr) -> set:
    try:
        return {t["uuid"] for t in board.read(pr, what=["tracks"]).get("track_list") or []}
    except ToolError:
        return set()


def polish(pr, before_uuids=None, drc_before=None, while_open="refuse", all_tracks=False, nets=None, silk=True,
           silk_hide="never", steps=None, _fail_after=False) -> dict:
    """Tidy the tracks created since ``before_uuids`` (every unlocked track with all_tracks=True) and
    re-seat unreadable reference designators. The board is restored when DRC errors or unrouted
    connections rose. -> {'merged', 'chamfered', 'pad_entries', 'silk': {...}, 'reverted': reason|None, 'drc': {...}}"""
    ops = []
    if all_tracks:
        ops.append({"op": "polish", "all": True, "nets": nets or [], "steps": steps})
    else:
        now = track_uuids(pr)
        new = sorted(now - set(before_uuids or ()))
        if new:
            ops.append({"op": "polish", "uuids": new, "nets": nets or [], "steps": steps})
    if _fail_after and ops:
        ops[0]["fail_after"] = True
    if silk:
        ops.append({"op": "silk_tidy2", "hide": silk_hide})
    if not ops:
        return {"skipped": "no new tracks"}
    if drc_before is None:
        drc_before = board.drc(pr, parity=False, record=False)
    cp = projmod.checkpoint(pr, "before-polish")
    saved = pr.pcb.read_bytes()
    res = board.run_ops(pr, ops, while_open=while_open, label="before-polish")
    out = {}
    for o, r in zip(ops, res["results"]):
        if o["op"] == "polish":
            out.update({k: v for k, v in r.items() if k != "steps"})
        else:
            out["silk"] = {k: r[k] for k in ("moved", "crowded", "hidden", "shrunk", "bad_after") if r.get(k)}
    d = board.drc(pr, parity=True)
    if d["errors"] > drc_before["errors"] or d["unconnected"] > drc_before["unconnected"]:
        pr.pcb.write_bytes(saved)           # byte-for-byte: the polish result was worse
        out = {"reverted": f"polish raised DRC errors {drc_before['errors']} -> {d['errors']} or unrouted "
                           f"{drc_before['unconnected']} -> {d['unconnected']}; the board is as routed"}
        d = board.drc(pr, parity=True)
    out["drc"] = d
    out["checkpoint"] = cp
    return out
