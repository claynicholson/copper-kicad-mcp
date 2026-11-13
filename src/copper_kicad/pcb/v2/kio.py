"""KiCad I/O for router v2 (design §4.11, §9): the pcbnew worker and kicad-cli DRC, no routing logic.

Thin wrappers over ``guided.session`` (``_run``, ``_scratch``, ``_raw_drc``, ``_sig``) so v2 talks to KiCad
exactly as v1 does: every board access is a short worker subprocess (``workers/guided_board.py``), the
judge is ``kicad-cli pcb drc --refill-zones``. Nothing here touches the user's board unless the caller
passes it as ``scratch``.

    extract(pr, ripup, nets)                         the worker's extract dict (rip-up applied in memory only)
    scratch(pr) / cleanup(sc)                        a temp copy of the project / remove it
    baseline_drc(pr, ripup, nets)                    raw DRC violations of the board before any new copper
    commit_and_check(pr, sc, tracks, vias, base)     write copper to the scratch board, refill, DRC, diff
    new_markers(raw, baseline_raw)                   the new errors, as markers that keep the item uuids
    marker_pieces(marker, uuids, piece_ids, state)   which pieces a marker blames (uuid, else proximity)

Marker format (a superset of what ``session._new_errors`` returns, so ``drc.new_top`` keeps its shape):

    {"type", "message", "items": [description, ...] (first two), "at": [x, y] (when known),
     "uuids": [uuid of every item], "pos": [[x, y] of every item], "copper": type in session.COPPER_TYPES}
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path

from ..guided import geom
from ..guided import session as _s

BLAME_RADIUS = 0.3        # mm: "pieces within 0.3 mm of the marker" (design §4.11)


def extract(pr, ripup: str = "none", nets: list[str] | None = None) -> dict:
    """The worker's ``extract`` of the board with the rip-up applied in memory (nothing is saved).
    The number of ripped items is added as ``ex["_ripped"]``. Feed it to ``guided.model.Board``."""
    _s._need_board(pr)
    res = _s._run(pr.pcb, [{"op": "ripup", "mode": ripup, "nets": list(nets or [])}, {"op": "extract"}], save=False)
    ex = res[-1]
    ex["_ripped"] = res[0].get("removed", 0)
    return ex


def scratch(pr):
    """A temp copy of the project (board, project file, rules, sheets): ``session._scratch``."""
    return _s._scratch(pr)


def cleanup(sc) -> None:
    """Remove a scratch project made by ``scratch()``."""
    if sc is not None:
        shutil.rmtree(sc.dir, ignore_errors=True)


def baseline_drc(pr, ripup: str = "none", nets: list[str] | None = None) -> list[dict]:
    """Raw kicad-cli violations (zones refilled) of the board as it is before routing — with the rip-up
    applied on a scratch copy when there is one. This is ``baseline_raw`` of commit_and_check."""
    if ripup == "none":
        return _s._raw_drc(pr)
    sc = scratch(pr)
    try:
        _s._run(sc.pcb, [{"op": "ripup", "mode": ripup, "nets": list(nets or [])}, {"op": "fill"}], save=True)
        return _s._raw_drc(sc)
    finally:
        cleanup(sc)


def _pos(item) -> list | None:
    p = item.get("pos") or {}
    return [p["x"], p["y"]] if "x" in p and "y" in p else None


def new_markers(raw: list[dict], baseline_raw: list[dict]) -> list[dict]:
    """DRC errors in ``raw`` that are not in ``baseline_raw`` (multiset difference on type + text + item
    texts, exactly ``session._new_errors``), as markers that keep the uuids and positions of their items.
    Canonical order: (type, x, y, message)."""
    have = {}
    for v in baseline_raw or []:
        if v.get("severity") == "error":
            k = _s._sig(v)
            have[k] = have.get(k, 0) + 1
    out = []
    for v in raw or []:
        if v.get("severity") != "error":
            continue
        k = _s._sig(v)
        if have.get(k):
            have[k] -= 1
            continue
        items = v.get("items") or []
        m = {"type": v.get("type"), "message": v.get("description"),
             "items": [i.get("description") for i in items][:2],
             "uuids": [i.get("uuid") for i in items if i.get("uuid")],
             "pos": [p for p in (_pos(i) for i in items) if p],
             "copper": v.get("type") in _s.COPPER_TYPES}
        # where to look: a pad / track / via rather than a zone (whose position is a corner of its outline)
        its = sorted(items, key=lambda i: str(i.get("description", "")).startswith("Zone"))
        at = _pos(its[0]) if its else None
        if at:
            m["at"] = at
        out.append(m)
    out.sort(key=lambda m: (str(m["type"]), (m.get("at") or [0.0, 0.0])[0], (m.get("at") or [0.0, 0.0])[1],
                            str(m["message"])))
    return out


def commit_and_check(pr, scratch, tracks: list, vias: list, baseline_raw: list, *, ripup: str = "none",
                     rip_nets: list[str] | None = None, fresh: bool = True, remove: list[str] | None = None,
                     drc: bool = True) -> dict:
    """Write copper to the scratch board, let KiCad refill the zones and judge it.

    ``tracks`` / ``vias`` are ``RouteState.export()``'s first two lists. With ``fresh`` (default) the scratch
    board is first reset to ``pr``'s board file, so after the call it holds exactly: the original board, minus
    the rip-up, plus this copper — call it once per judge round with the whole state. ``fresh=False`` adds to
    what the scratch board already holds (``remove``: uuids to delete first), for incremental passes.

    -> {"uuids": track uuids + via uuids, in input order (zip with export()'s id list),
        "track_uuids", "via_uuids",
        "raw": every kicad-cli violation, "new": new_markers(raw, baseline_raw)  ([] / [] with drc=False),
        "unconnected": {net: n}, "stats": the worker's stats (totals, net_stats, unconnected),
        "ex": the extract of the committed, refilled board (for a pours pass)}
    """
    sc_pcb = Path(scratch.pcb)
    if fresh and pr is not None and Path(pr.pcb).resolve() != sc_pcb.resolve():
        shutil.copy2(pr.pcb, sc_pcb)
    ops = []
    if ripup != "none" and fresh:
        ops.append({"op": "ripup", "mode": ripup, "nets": list(rip_nets or [])})
    if remove:
        ops.append({"op": "remove", "uuids": list(remove)})
    n_pre = len(ops)
    ops += [{"op": "commit", "tracks": tracks, "vias": vias}, {"op": "extract"}, {"op": "stats"}]
    res = _s._run(sc_pcb, ops, save=True)
    cm, ex, st = res[n_pre], res[n_pre + 1], res[n_pre + 2]
    raw = _s._raw_drc(scratch) if drc else []
    return {"uuids": list(cm["track_uuids"]) + list(cm["via_uuids"]),
            "track_uuids": list(cm["track_uuids"]), "via_uuids": list(cm["via_uuids"]),
            "raw": raw, "new": new_markers(raw, baseline_raw) if drc else [],
            "unconnected": st.get("unconnected_by_net") or {}, "stats": st, "ex": ex}


def _marker_uuids(marker: dict) -> list[str]:
    out = list(marker.get("uuids") or [])
    for i in marker.get("items") or []:
        if isinstance(i, dict) and i.get("uuid") and i["uuid"] not in out:
            out.append(i["uuid"])
    return out


def _marker_points(marker: dict) -> list[tuple[float, float]]:
    pts = []
    for p in marker.get("pos") or []:
        pts.append((p[0], p[1]))
    for i in marker.get("items") or []:
        if isinstance(i, dict):
            p = _pos(i)
            if p:
                pts.append((p[0], p[1]))
    at = marker.get("at")
    if at and len(at) >= 2 and at[0] is not None:
        pts.append((at[0], at[1]))
    out = []
    for p in pts:
        if p not in out:
            out.append(p)
    return out


def marker_pieces(marker: dict, uuids: list[str], piece_ids: list[int], state, radius: float = BLAME_RADIUS) -> list[int]:
    """Piece ids a DRC marker blames, sorted: the pieces whose uuid is among the marker's items
    (``uuids[i]`` is the board item of ``piece_ids[i]``: commit_and_check's ``uuids`` and export()'s id
    list); if none of them is ours, every piece of the state whose copper comes within ``radius`` mm of
    a marker position. ``marker``: a marker of new_markers() or a raw kicad-cli violation."""
    by_uuid = dict(zip(uuids, piece_ids))
    hit = sorted({by_uuid[u] for u in _marker_uuids(marker) if u in by_uuid and state.piece(by_uuid[u]) is not None})
    if hit:
        return hit
    pts = _marker_points(marker)
    if not pts:
        return []
    out = []
    for p in state.pieces():
        r = p.w / 2 + radius
        for x, y in pts:
            if p.kind == "via":
                d = math.hypot(p.x1 - x, p.y1 - y)
            else:
                if x < min(p.x1, p.x2) - r or x > max(p.x1, p.x2) + r or y < min(p.y1, p.y2) - r or y > max(p.y1, p.y2) + r:
                    continue
                d = geom.pt_seg_dist(x, y, p.x1, p.y1, p.x2, p.y2)
            if d <= r:
                out.append(p.id)
                break
    return out
