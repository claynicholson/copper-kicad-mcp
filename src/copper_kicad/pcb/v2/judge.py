"""S7 judge: commit, refill, DRC, blame, local re-route, keep best, pours pass (design §4.11).

KiCad is the judge. The whole route state is committed to a scratch board (always from the same *base* file:
the board without any v2 copper), KiCad refills the zones and runs DRC, and every *new* error is blamed on the
connection whose copper takes part in it. Only that connection is removed and re-routed (never a net, never a
restart). The best round by ``Score`` is kept: a later round can never make the returned board worse.

    run(ctx, budget, pr, scratch, baseline_raw, reroute)   ≤ 5 commit+DRC rounds, ≤ 4 local re-routes,
                                                           then removal of whatever is still blamed
    pours_pass(ctx, pr, scratch, budget)                   ≤ 2 rounds: pads a refilled pour does not reach
    tune_pass(ctx, pr, scratch, budget)                    length tuning (v2/tune.py), kept only when KiCad's DRC
                                                           gains no error and no open connection

No routing search lives here: re-routing goes through the ``reroute`` callable the engine hands in; KiCad I/O
goes through ``kio``. The judge talks to the search through three channels (interface notes, W2):
``ctx.history['marks']``, cost / forbid regions named "judge" in ``ctx.cfg.regions``, and hardened soft shapes
in the oracle (starved thermal reliefs).

Bookkeeping shared with the engine lives in ``ctx.history['judge']`` (see ``_jst``).
"""
from __future__ import annotations

import copy
import math
import re
import shutil
from pathlib import Path
from typing import Callable

from ..guided import geom, model, plan as planmod
from . import constraints, kio
from .types import Budget, Ctx, Piece, Score, StageReport, q

ROUNDS = 4                # local re-route rounds after the first check (design: for round in 0..4)
REMOVE_ROUNDS = 3         # removal passes when the best round still has blamed errors
POUR_ROUNDS = 4          # plain rounds while each one pays (a stub or stitch via opens the way for the next)
POUR_CULPRITS = 3         # pours pass: removal rounds for stubs KiCad rejects, before the whole round is undone
POUR_BLAME_MM = 5.0       # ... how far from a marker a new stub is still a suspect
GIVE_ROOM = 2             # pours pass: how often crowded pour pads are given room (re-route of the crowd)
TUNE_ROUNDS = 3           # length tuning: tuner runs KiCad judges, each followed by one commit + DRC (design §14.6)
TUNE_BLAME_MM = 1.0       # ... how far from a marker a meander is still the suspect when the marker names no item
TUNE_AVOID_MM = 1.0       # ... half size of the square closed to that net's meanders round a marker KiCad raised
POUR_RIPS = 8             # pours pass: bridges that move routed connections out of the way, tried by KiCad per run
HIST_RADIUS = 1.0         # mm: history / cost region around a marker
HIST_DECAY = 0.95
COST_FIRST = 3.0          # region cost the first time a connection is blamed at a place
COST_PAD = 8.0            # ... when a forbid is impossible because the net has a pad there
FORBID_RADIUS = 0.4       # mm: half size of the forbid square the second time
WIDEN_STARVED = 1.5       # mm: second look around a starved pad (a track can cut the pour off further out)
EPS_JOIN = 2e-6           # two piece ends are the same point
SPOKE_BLAME = True        # a starved pad that lacks N spokes: first free only the N cheapest spoke corridors
SPOKE_MARGIN = 0.05       # mm added to "this copper keeps the pour out of the spoke corridor"
SPOKE_GROW = 0.3          # mm the forbid rectangle of a protected spoke corridor is grown by (track half width + gap)
THERMAL_COPPER = True     # a starved thermal relief is first repaired with copper (a spoke track / a pour stub)
SPOKE_TRACK_W = 0.5       # mm: widest spoke track (the net's own width when that is smaller)
SPOKE_TRACK_REACH = 4.0   # mm past the pad edge a spoke track may run to find the pour
ISLAND_MIN_AREA = 10.0    # mm2: a piece of pour at least this large counts as "the rest of the pour"
ISLAND_TRACK_REACH = 8.0  # ... and to find the rest of the pour from a pad on a cut-off island
SPOKE_TRACK_GAP = 0.2     # mm past the pad edge: pour found here already = this direction has its spoke
_SPOKES_RE = re.compile(r"min spoke count (\d+); actual (\d+)")
_LAYER_RE = re.compile(r"layer ([^;)]+)")


def _copper_types() -> set:
    from ..guided import session        # lazy: session imports the engine lazily too
    return session.COPPER_TYPES


# ── shared bookkeeping ────────────────────────────────────────────────────
def _jst(ctx: Ctx) -> dict:
    """ctx.history['judge']:
      base, best_file   Path | None   the scratch board without v2 copper / the file of the best round
      commits           int           commit+DRC round trips so far
      blamed            {(key, type): n}   how often a connection was blamed for a rule (drives the escalation)
      removed           {key: (net, why)}  connections / groups removed and not re-routed
      protected         {pad name: [rect]} pads that were given room (starved thermal, unreachable pour pad)
      repairs           [str]              human-readable list of the above
      last              the accepted check: {new, raw, unconnected, stats, uuids, pids, score}
      pour_failed       [v1-format failed dicts of the last pours round]
      pour_pieces       [piece ids added by the pours pass]
      rounds            [{round, new_errors, opens, removed, rerouted}]
    """
    j = ctx.history.get("judge")
    if j is None:
        j = ctx.history["judge"] = {"base": None, "best_file": None, "commits": 0, "blamed": {}, "removed": {},
                                    "protected": {}, "repairs": [], "last": None, "pour_failed": [],
                                    "pour_pieces": [], "rounds": [], "unblamed": []}
    return j


def _prog(ctx: Ctx, f: float, msg: str) -> None:
    if ctx.progress:
        try:
            ctx.progress(f, msg)
        except Exception:  # noqa: BLE001 — progress is cosmetic
            pass


def fail_of(state, idx: int) -> dict | None:
    """The failure record stored by set_status(conn, 'failed', fail) (§9 has no getter: interface notes W2.2)."""
    for name in ("fail", "failure"):
        f = getattr(state, name, None)
        if callable(f):
            try:
                return f(idx)
            except Exception:  # noqa: BLE001
                return None
    d = getattr(state, "fails", None)
    if isinstance(d, dict):
        return d.get(idx)
    return None


def is_target(ctx: Ctx, net: str) -> bool:
    """Does this run route the net? Not skipped by the plan, or classify made connections for it anyway
    (a ground without a pour that S0 routes as power). The engine pins the set in ctx.history['targets']."""
    t = ctx.history.get("targets")
    if t is not None:
        return net in t
    nc = ctx.cfg.nets.get(net)
    return nc is not None and not nc.skip


def targeted_opens(ctx: Ctx, unconnected) -> int | None:
    """KiCad's unconnected count over the nets this run routes (None when kio gave no per-net numbers)."""
    if not isinstance(unconnected, dict):
        return None
    return sum(int(v) for n, v in unconnected.items() if is_target(ctx, n))


# ── scratch files ─────────────────────────────────────────────────────────
def _pcb(scratch) -> Path | None:
    p = getattr(scratch, "pcb", None)
    return Path(p) if p else None


def _copy(src: Path | None, dst: Path | None) -> None:
    if src and dst and Path(src).exists():
        shutil.copy2(src, dst)


def _ensure_base(ctx: Ctx, scratch) -> None:
    """Remember the scratch board as it is now (no v2 copper) so every round commits to the same board."""
    j = _jst(ctx)
    pcb = _pcb(scratch)
    if j["base"] is None and pcb is not None and pcb.exists():
        j["base"] = pcb.with_name(pcb.name + ".v2base")
        j["best_file"] = pcb.with_name(pcb.name + ".v2best")
        shutil.copy2(pcb, j["base"])


def cleanup(ctx: Ctx) -> None:
    """Delete the judge's helper files next to the scratch board (the engine calls this when it is done)."""
    j = ctx.history.get("judge") or {}
    for k in ("base", "best_file"):
        p = j.get(k)
        if p:
            try:
                Path(p).unlink()
            except OSError:
                pass


# ── one commit + DRC round trip ───────────────────────────────────────────
def marker_key(m: dict) -> tuple:
    """Canonical marker order (§7): type, x, y, description."""
    at = marker_at(m) or (0.0, 0.0)
    return (str(m.get("type")), round(at[0], 4), round(at[1], 4), str(m.get("message") or m.get("description") or ""))


def marker_at(m: dict):
    at = m.get("at")
    if isinstance(at, (list, tuple)) and len(at) >= 2 and at[0] is not None and at[1] is not None:
        return float(at[0]), float(at[1])
    pos = m.get("pos")
    if isinstance(pos, dict) and "x" in pos:
        return float(pos["x"]), float(pos["y"])
    its = [i for i in (m.get("items") or []) if isinstance(i, dict) and isinstance(i.get("pos"), dict) and "x" in i["pos"]]
    its.sort(key=lambda i: str(i.get("description", "")).startswith("Zone"))      # stable: a zone's pos is an outline corner
    if its:
        return float(its[0]["pos"]["x"]), float(its[0]["pos"]["y"])
    return None


def _check(ctx: Ctx, pr, scratch, baseline_raw) -> dict:
    """Commit the whole state to the base board, let KiCad refill and judge. -> the check record."""
    j = _jst(ctx)
    _ensure_base(ctx, scratch)
    _copy(j["base"], _pcb(scratch))
    tracks, vias, pids = ctx.state.export()
    # kio resets the scratch board from the project it is given (fresh=True): hand it the base file, never pr's
    # board (which may still hold the copper a rip-up removed)
    res = kio.commit_and_check(_BaseRef(pr, j["base"]) if j["base"] else pr, scratch, tracks, vias, baseline_raw)
    j["commits"] += 1
    every = sorted(res.get("new") or [], key=marker_key)
    # KiCad's length / skew / pair / via-count markers are constraint verdicts, not copper errors: ripping a
    # connection up never makes a net longer or a pair better matched, so they are never blamed and never in the
    # score. They are read from the raw list (a `length (min ..)` rule already fails on the unrouted board with
    # "actual 0" and changes its text once routed) and reported per group (design §14.6)
    new = [m for m in every if m.get("type") not in constraints.KICAD_TYPES]
    _dump(j, scratch, new)
    chk = {"new": new, "raw": res.get("raw") or [], "unconnected": res.get("unconnected"), "stats": res.get("stats") or {},
           "uuids": list(res.get("uuids") or []), "pids": list(pids), "ex": res.get("ex"),
           "cons": constraints.kicad_markers(res.get("raw") or []), "cons_new": len(every) - len(new)}
    s = ctx.state.score()
    opens = targeted_opens(ctx, chk["unconnected"])
    if opens is not None and isinstance(chk["unconnected"], dict):
        # KiCad's ratsnest also counts copper that hangs on no pad (a via left in a pour that has moved away):
        # the per-net figure is by pad groups and does not see it, the board total does
        try:
            extra = int((chk["stats"] or {}).get("unconnected") or 0) - sum(int(v) for v in chk["unconnected"].values())
        except (TypeError, ValueError):
            extra = 0
        chk["orphans"] = max(0, extra)
        opens += chk["orphans"]
    chk["score"] = Score(new_errors=len(new), opens=s.opens if opens is None else opens, necked_mm=s.necked_mm,
                         vias=s.vias, length_mm=s.length_mm, bends=s.bends)
    ctx.state.new_errors = len(new)
    return chk


def _dump(j: dict, scratch, new: list) -> None:
    """Diagnostics only (off unless COPPER_V2_DUMP names a directory): keep every committed board and its new
    markers, so a judge round can be looked at in KiCad afterwards. Never read back."""
    import os
    d = os.environ.get("COPPER_V2_DUMP")
    pcb = _pcb(scratch)
    if not d or pcb is None:
        return
    try:
        import json
        Path(d).mkdir(parents=True, exist_ok=True)
        n = j["commits"]
        shutil.copy2(pcb, Path(d) / f"commit{n:02d}.kicad_pcb")
        (Path(d) / f"commit{n:02d}.json").write_text(json.dumps(new, default=str, indent=1), encoding="utf-8")
    except OSError:
        pass


class _BaseRef:
    """Looks like the project to kio.commit_and_check, but its board file is the judge's base file."""

    def __init__(self, pr, base):
        self.pcb = Path(base)
        self.dir = getattr(pr, "dir", None)
        self.name = getattr(pr, "name", None)


class _Best:
    """The best round so far: state snapshot + its scratch file + the bookkeeping that belongs to it."""

    def __init__(self, ctx, scratch, chk, rnd):
        j = _jst(ctx)
        self.snap = ctx.state.snapshot()
        self.chk = chk
        self.score = chk["score"]
        self.round = rnd
        self.removed = dict(j["removed"])
        _copy(_pcb(scratch), j["best_file"])

    def restore(self, ctx, scratch):
        j = _jst(ctx)
        ctx.state.restore(self.snap)
        ctx.state.new_errors = self.score.new_errors
        j["removed"] = dict(self.removed)
        j["last"] = self.chk
        _copy(j["best_file"], _pcb(scratch))


# ── blame ─────────────────────────────────────────────────────────────────
def _pieces_by_id(state) -> dict:
    return {p.id: p for p in state.pieces()}


def _group(state, p: Piece, by_id: dict | None = None) -> list[Piece]:
    """What is removed together with piece p: its connection, or — for conn == -1 (reservations, pour stubs) —
    the same-net conn == -1 pieces chained to it by shared end points."""
    if p.conn >= 0:
        return list(state.pieces(conn=p.conn))
    loose = [o for o in state.pieces(net=p.net) if o.conn < 0]
    group, front = {p.id: p}, [p]
    while front:
        a = front.pop()
        for o in loose:
            if o.id in group:
                continue
            if any(abs(ax - ox) <= EPS_JOIN and abs(ay - oy) <= EPS_JOIN
                   for ax, ay in ((a.x1, a.y1), (a.x2, a.y2)) for ox, oy in ((o.x1, o.y1), (o.x2, o.y2))):
                group[o.id] = o
                front.append(o)
    return [group[i] for i in sorted(group)]


def _key(state, p: Piece):
    """Removal unit of a piece: the connection index, or ('g', smallest piece id of its loose group)."""
    return p.conn if p.conn >= 0 else ("g", _group(state, p)[0].id)


def _late(ctx: Ctx, p: Piece) -> tuple:
    """Sort key 'routed later = removed first' (v1: of two new items, drop the one routed later)."""
    if p.conn >= 0:
        order = ctx.history.get("_judge_order")
        if order is None or len(order) != len(ctx.conns):
            ranked = sorted(ctx.conns, key=lambda c: (c.rank, c.idx))
            order = ctx.history["_judge_order"] = {c.idx: i for i, c in enumerate(ranked)}
        return (1, order.get(p.conn, p.conn), p.id)
    if p.protected or p.stage in ("power", "fanout"):
        return (0, 0, p.id)             # reservations were placed first: the last to go
    return (2, 0, p.id)                 # pour stubs were added last


def _nearest_on(p: Piece, x: float, y: float) -> tuple[float, float]:
    dx, dy = p.x2 - p.x1, p.y2 - p.y1
    d2 = dx * dx + dy * dy
    if p.kind == "via" or d2 < 1e-18:
        return p.x1, p.y1
    t = max(0.0, min(1.0, ((x - p.x1) * dx + (y - p.y1) * dy) / d2))
    return p.x1 + t * dx, p.y1 + t * dy


def _other_pos(m: dict, uuids: set):
    """Position of the first marker item that is not our copper and not a zone (kio markers: parallel 'uuids' /
    'pos' / 'items' lists; raw kicad-cli violations: item dicts)."""
    items = m.get("items") or []
    if items and isinstance(items[0], dict):
        for it in items:
            pos = it.get("pos")
            if it.get("uuid") not in uuids and isinstance(pos, dict) and "x" in pos \
                    and not str(it.get("description", "")).startswith("Zone"):
                return float(pos["x"]), float(pos["y"])
        return None
    uu, pos = m.get("uuids") or [], m.get("pos") or []
    if len(uu) != len(pos):
        return None
    for k, (u, p) in enumerate(zip(uu, pos)):
        zone = k < len(items) and str(items[k]).startswith("Zone")
        if u not in uuids and not zone and isinstance(p, (list, tuple)) and len(p) >= 2:
            return float(p[0]), float(p[1])
    return None


def _where(m: dict, mine: list[Piece], uuids: set) -> tuple[float, float]:
    """Where on our copper the violation most likely is: the point of the blamed piece nearest to the other
    item of the marker (a pad / via / track of somebody else), else nearest to the marker itself."""
    ref = _other_pos(m, uuids) or marker_at(m)
    p = mine[0]
    if ref is None:
        return (p.x1 + p.x2) / 2, (p.y1 + p.y2) / 2
    best = None
    for o in mine:
        x, y = _nearest_on(o, ref[0], ref[1])
        d = math.hypot(x - ref[0], y - ref[1])
        if best is None or d < best[0] - 1e-12:
            best = (d, x, y)
    return best[1], best[2]


def _why(m: dict) -> str:
    at = marker_at(m)
    s = f"KiCad DRC {m.get('type')}: {m.get('message') or m.get('description') or ''}".rstrip(": ")
    if at:
        s += f" at ({round(at[0], 3)}, {round(at[1], 3)})"
    return s


def _pad_at(board, x, y):
    for p in board.pads:
        if abs(p.x - x) < 0.01 and abs(p.y - y) < 0.01 and p.net:
            return p
    return None


def _pad_rect(pad) -> tuple:
    """Room a pad needs to reach its pour: its own extent plus the thermal gap and a spoke (v1 session._pad_rect)."""
    ex_, ey_ = 0.25, 0.25
    for shapes in pad.shapes.values():
        for s in shapes:
            x0, y0, x1, y1 = s.bbox
            ex_ = max(ex_, abs(x0 - pad.x), abs(x1 - pad.x))
            ey_ = max(ey_, abs(y0 - pad.y), abs(y1 - pad.y))
    mx = 1.3 if ex_ <= 0.8 else min(ex_, 3.0) + 0.75
    my = 1.3 if ey_ <= 0.8 else min(ey_, 3.0) + 0.75
    return (round(pad.x - mx, 3), round(pad.y - my, 3), round(pad.x + mx, 3), round(pad.y + my, 3))


def _in_rect(p: Piece, rect) -> bool:
    x0, y0, x1, y1 = rect
    if p.kind == "via":
        return x0 <= p.x1 <= x1 and y0 <= p.y1 <= y1
    if max(p.x1, p.x2) < x0 or min(p.x1, p.x2) > x1 or max(p.y1, p.y2) < y0 or min(p.y1, p.y2) > y1:
        return False
    steps = max(1, int((abs(p.x2 - p.x1) + abs(p.y2 - p.y1)) / 0.2))
    for k in range(steps + 1):
        x, y = p.x1 + (p.x2 - p.x1) * k / steps, p.y1 + (p.y2 - p.y1) * k / steps
        if x0 <= x <= x1 and y0 <= y <= y1:
            return True
    return False


def _region(cfg, rect, *, forbid: bool, cost: float, nets=None, exc=None) -> None:
    r = planmod.Region()
    r.idx = 1000 + len(cfg.regions)
    r.rect, r.forbid, r.cost, r.layers = tuple(rect), forbid, cost, None
    r.nets, r.exc, r.name = (set(nets) if nets is not None else None), set(exc or ()), "judge"
    cfg.regions.append(r)


def _net_name(board, nid) -> str | None:
    n = board.net_by_id.get(nid)
    return n.name if n else None


def _pads_in(board, rect) -> set:
    x0, y0, x1, y1 = rect
    return {_net_name(board, p.net) for p in board.pads if p.net and x0 <= p.x <= x1 and y0 <= p.y <= y1}


def _protect_pad(ctx: Ctx, pad, net: str, rect, spokes: bool = True) -> None:
    """Keep other nets out of a pad's neighbourhood: plan regions (what v1's search and every region-aware search
    reads) and, for a starved thermal relief (``spokes``), the pad's spoke corridors as hard shapes in the oracle
    when the power module can give them."""
    inside = _pads_in(ctx.board, rect) - {net}
    _region(ctx.cfg, rect, forbid=True, cost=1.0, exc=inside | {net})
    if inside:
        _region(ctx.cfg, rect, forbid=False, cost=COST_PAD, nets=inside)
    if not spokes:
        return
    try:
        booked = (ctx.history.get("power") or {}).get("spokes") or {}
        ids = sorted(sid for sid, v in booked.items() if v and v[0] == pad.name)
        if ids:                             # S1 registered them as soft shapes: close them
            for sid in ids:
                ctx.oracle.harden(sid)
            return
        from . import power
        if not getattr(power, "STUB", False):
            for sh in power.thermal_spokes(ctx.board, pad) or []:
                for lay in (getattr(sh, "layers", None) or (-1,)):
                    ctx.oracle.harden(ctx.oracle.add_soft("thermal", net, lay, sh))
    except Exception:  # noqa: BLE001 — the regions above already carry the constraint
        pass


def _spoke_ends(sh) -> tuple:
    cx, cy, ux, uy, hl = sh.g[0], sh.g[1], sh.g[2], sh.g[3], sh.g[4]
    return cx - ux * hl, cy - uy * hl, cx + ux * hl, cy + uy * hl


def _spoke_crowd(ctx: Ctx, pad, net: str, m: dict, by_id: dict):
    """Spoke-level blame for 'zone min spoke count N; actual K': a thermal relief needs N - K more spokes, not the
    whole neighbourhood of the pad. -> (pieces to remove, [spoke corridor shapes to keep free], layer) or None
    (no spoke geometry, not this kind of marker, or not enough spokes that this run's copper alone closes: the
    caller then falls back to the pad's rectangle).

    A spoke corridor (power.thermal_spokes: pad edge, thermal gap, a little of the pour) is closed by a piece of
    another net when the piece's copper plus its clearance reaches into it. The spokes closed by the fewest
    connections are freed; they and the spokes nothing of ours touches are then kept free for good."""
    msg = str(m.get("message") or m.get("description") or "")
    mt = _SPOKES_RE.search(msg)
    if not mt:
        return None
    lack = int(mt.group(1)) - int(mt.group(2))
    if lack <= 0:
        return None
    try:
        from . import power
        if getattr(power, "STUB", False):
            return None
        spokes = list(power.thermal_spokes(ctx.board, pad) or [])
    except Exception:  # noqa: BLE001 — no geometry: the rectangle rule applies
        return None
    lm = _LAYER_RE.search(msg)
    names = list(getattr(ctx.board, "layers", ()) or ())
    if lm and lm.group(1).strip() in names:
        lay = names.index(lm.group(1).strip())
        spokes = [sh for sh in spokes if lay in sh.layers]
    if not spokes or len({sh.layers[0] for sh in spokes}) != 1:
        return None
    lay = spokes[0].layers[0]
    o, state = ctx.oracle, ctx.state
    ours = frozenset(by_id)
    free, closed = [], []
    for k, sh in enumerate(spokes):
        ax, ay, bx, by = _spoke_ends(sh)
        hw = sh.g[5]
        try:
            static = o.seg_ok(lay, ax, ay, bx, by, 2 * hw, net, ignore=ours)
        except Exception:  # noqa: BLE001
            static = None
        if static is not None:
            continue                        # a pad / hole / edge closes this spoke: removing our copper cannot open it
        hit = []
        for p in by_id.values():
            if p.net == net:
                continue
            if p.kind == "via":
                d = geom.pt_seg_dist(p.x1, p.y1, ax, ay, bx, by)
            elif p.layer != lay:
                continue
            else:
                d = geom.seg_seg_dist(p.x1, p.y1, p.x2, p.y2, ax, ay, bx, by)
            if d < hw + p.w / 2 + o.clearance(p.net) + SPOKE_MARGIN:
                hit.append(p)
        if hit:
            keys = sorted({str(_key(state, p)) for p in hit})
            closed.append((len(keys), sum(p.length for p in hit), k, sh, hit))
        else:
            free.append(sh)
    if len(closed) < lack:
        return None
    closed.sort(key=lambda r: r[:3])
    crowd, keep = {}, list(free)
    for _n, _len, _k, sh, hit in closed[:lack]:
        keep.append(sh)
        for p in hit:
            crowd[p.id] = p
    _jst(ctx).setdefault("spoke_blame", []).append(
        {"pad": pad.name, "lack": lack, "free": len(free), "closed": [r[0] for r in closed],
         "freed": sorted({str(_key(state, p)) for p in crowd.values()})})
    return [crowd[i] for i in sorted(crowd)], keep, lay


def _protect_spokes(ctx: Ctx, pad, net: str, keep: list) -> None:
    """Keep other nets out of these spoke corridors of a pad: forbid regions (grown by a track's half width and
    gap, for the grid searches) and hard shapes in the oracle (the exact rule)."""
    for sh in keep:
        x0, y0, x1, y1 = sh.bbox
        rect = (round(x0 - SPOKE_GROW, 3), round(y0 - SPOKE_GROW, 3), round(x1 + SPOKE_GROW, 3), round(y1 + SPOKE_GROW, 3))
        inside = _pads_in(ctx.board, rect) - {net}
        r0 = len(ctx.cfg.regions)
        _region(ctx.cfg, rect, forbid=True, cost=1.0, exc=inside | {net})
        if inside:
            _region(ctx.cfg, rect, forbid=False, cost=COST_PAD, nets=inside)
        for r in ctx.cfg.regions[r0:]:
            r.layers = set(sh.layers)
        try:
            for lay in sh.layers:
                ctx.oracle.harden(ctx.oracle.add_soft("thermal", net, lay, sh))
        except Exception:  # noqa: BLE001 — the regions above already carry the constraint
            pass


def _blame(ctx: Ctx, chk: dict, fine: bool = False) -> tuple[dict, list]:
    """-> ({key: victim}, [unblamed marker]) for the new errors of a check.
    victim = {pieces, conn, net, why, at, type, starved: pad | None}"""
    state, j = ctx.state, _jst(ctx)
    by_id = _pieces_by_id(state)
    uu = set(chk["uuids"])
    copper = _copper_types()
    victims, unblamed = {}, []

    def add(p: Piece, m, at, starved=None, spokes=None):
        k = _key(state, p)
        if k in victims:
            return
        victims[k] = {"pieces": _group(state, p, by_id), "conn": p.conn, "net": p.net, "why": _why(m), "at": at,
                      "type": str(m.get("type")), "starved": starved, "blamed_id": p.id, "spokes": spokes}

    for m in chk["new"]:
        t = m.get("type")
        if t == "starved_thermal":
            at = marker_at(m)
            pad = _pad_at(ctx.board, at[0], at[1]) if at else None
            if pad is None:
                unblamed.append(m)
                continue
            net = _net_name(ctx.board, pad.net)
            if fine and SPOKE_BLAME and not j["protected"].get(pad.name):
                # first time this pad is starved: free only the spokes it lacks (the rectangle comes next time)
                sp = _spoke_crowd(ctx, pad, net, m, by_id)
                if sp:
                    for p in sp[0]:
                        add(p, m, (pad.x, pad.y), starved=(pad, net, ("spokes", sp[2])), spokes=sp[1])
                    continue
            rect = _pad_rect(pad)
            crowd = [p for p in by_id.values() if p.net != net and _in_rect(p, rect)]
            if not crowd:
                rect = (rect[0] - WIDEN_STARVED, rect[1] - WIDEN_STARVED, rect[2] + WIDEN_STARVED, rect[3] + WIDEN_STARVED)
                crowd = [p for p in by_id.values() if p.net != net and _in_rect(p, rect)]
            if not crowd:
                unblamed.append(m)          # not caused by copper we added (footprint / placement)
                continue
            for p in sorted(crowd, key=lambda p: p.id):
                add(p, m, (pad.x, pad.y), starved=(pad, net, rect))
            continue
        if t not in copper:
            unblamed.append(m)
            continue
        ids = [i for i in (kio.marker_pieces(m, chk["uuids"], chk["pids"], state) or []) if i in by_id]
        if not ids:
            unblamed.append(m)
            continue
        mine = [by_id[i] for i in sorted(ids)]
        keys = {}
        for p in mine:
            keys.setdefault(_key(state, p), []).append(p)
        if any(k in victims for k in keys):
            continue                        # one of its connections already goes: that clears a two-item marker
        pick = max(mine, key=lambda p: _late(ctx, p))
        add(pick, m, _where(m, keys[_key(state, pick)], uu))
    j["unblamed"] = [{"type": m.get("type"), "message": m.get("message") or m.get("description"),
                      "at": list(marker_at(m) or ()) or None} for m in unblamed]
    return victims, unblamed


def _feedback(ctx: Ctx, key, v: dict) -> bool:
    """Tell the search why this connection was removed. -> False when it should not be re-routed again."""
    j = _jst(ctx)
    net, (x, y) = v["net"], v["at"]
    if v["starved"]:
        pad, pnet, rect = v["starved"]
        seen = j["protected"].setdefault(pad.name, [])
        if rect in seen:
            # closed in an earlier round and the pad is starved again: the crowd goes. Closed for another
            # connection of this same round: this one is re-routed with it.
            return (pad.name, rect) in j.get("fresh", ())
        if not seen:
            j["repairs"].append(f"gave room to the {pnet} pad {pad.name} at ({round(pad.x, 2)}, {round(pad.y, 2)})")
        seen.append(rect)                   # the close rectangle first, the wide one the next time (v1 does the same)
        j.setdefault("fresh", set()).add((pad.name, rect))
        if v.get("spokes") is not None:
            _protect_spokes(ctx, pad, pnet, v["spokes"])
        else:
            _protect_pad(ctx, pad, pnet, rect)
        return True
    n = j["blamed"][(key, v["type"])] = j["blamed"].get((key, v["type"]), 0) + 1
    marks = ctx.history.setdefault("marks", [])
    marks.append([q(x), q(y), HIST_RADIUS, 1.0, net])
    if n == 1:
        _region(ctx.cfg, (x - HIST_RADIUS, y - HIST_RADIUS, x + HIST_RADIUS, y + HIST_RADIUS), forbid=False,
                cost=COST_FIRST, nets={net})
        return True
    if n == 2:
        rect = (x - FORBID_RADIUS, y - FORBID_RADIUS, x + FORBID_RADIUS, y + FORBID_RADIUS)
        if net in _pads_in(ctx.board, rect):
            _region(ctx.cfg, (x - HIST_RADIUS, y - HIST_RADIUS, x + HIST_RADIUS, y + HIST_RADIUS), forbid=False,
                    cost=COST_PAD, nets={net})
        else:
            _region(ctx.cfg, rect, forbid=True, cost=1.0, nets={net})
        return True
    return False                            # blamed three times for the same rule: stop trying


def _remove(ctx: Ctx, key, v: dict) -> None:
    state = ctx.state
    if v["conn"] >= 0:
        state.rip_conn(v["conn"])           # its escape stubs (protected) stay for the re-route ...
        if state_has(state, v.get("blamed_id", -1)):
            _rip_all(state, v["conn"])      # ... unless the blamed piece is one of them
        state.set_status(v["conn"], "todo")
    else:
        for p in v["pieces"]:
            state.remove(p.id)


def _rip_all(state, conn: int) -> None:
    try:
        state.rip_conn(conn, protected=True)
    except TypeError:                       # a state without the keyword: remove piece by piece
        for p in list(state.pieces(conn=conn)):
            state.remove(p.id)


def _mark_removed(ctx: Ctx, key, v: dict, note: str = "") -> None:
    """The connection stays out: report it (failed[] + removed_after_drc)."""
    j = _jst(ctx)
    j["removed"][key] = (v["net"], v["why"])
    if v["conn"] >= 0:
        c = ctx.conns[v["conn"]] if 0 <= v["conn"] < len(ctx.conns) and ctx.conns[v["conn"]].idx == v["conn"] else None
        old = fail_of(ctx.state, v["conn"]) if ctx.state.status(v["conn"]) == "failed" else None
        fail = dict(old or {})
        reason = "routed, then removed: " + v["why"]
        if note:
            reason += f" ({note})"
        elif old and old.get("reason") and not str(old["reason"]).startswith("routed, then removed"):
            reason += f" — the re-route failed: {old['reason']}"
        fail["reason"] = reason
        fail.setdefault("where", [round(v["at"][0], 2), round(v["at"][1], 2)])
        if c is not None:
            fail.setdefault("net", c.net)
        ctx.state.set_status(v["conn"], "failed", fail)


def _counts(ctx: Ctx) -> tuple[int, int]:
    st = [ctx.state.status(c.idx) for c in ctx.conns]
    return sum(1 for s in st if s == "routed"), sum(1 for s in st if s == "failed")


# ── starved thermal reliefs: copper first ─────────────────────────────────
_DIRS8 = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
          (0.7071067811865476, 0.7071067811865476), (-0.7071067811865476, 0.7071067811865476),
          (-0.7071067811865476, -0.7071067811865476), (0.7071067811865476, -0.7071067811865476))


def _marker_layer(ctx: Ctx, m: dict):
    lm = _LAYER_RE.search(str(m.get("message") or m.get("description") or ""))
    names = list(getattr(ctx.board, "layers", ()) or ())
    if lm and lm.group(1).strip() in names:
        return names.index(lm.group(1).strip())
    return None


def _pad2(b2, pad):
    for p in b2.pads:
        if p.name == pad.name and abs(p.x - pad.x) < 0.01 and abs(p.y - pad.y) < 0.01:
            return p
    return None


def _spoke_tracks(ctx: Ctx, b2, pad, net: str, lay: int, lack: int, island: bool = False) -> list[int]:
    """What a layout engineer does for a thermal relief that lacks spokes: short tracks from the pad centre across
    the thermal gap into the pour, in directions that have no spoke yet (KiCad counts a track from the pad into
    the zone fill as a spoke). ``b2`` is the committed, refilled board: its fill islands say where the pour is.
    Straight, axial or 45 degrees, every one legal by the oracle. -> ids of the pieces added (conn == -1).

    ``island``: the pad sits on a piece of the pour that is cut off from the rest. The track then has to reach the
    pour of the net's main cluster (one straight track from the pad into it both connects the pad and makes its
    relief count again)."""
    p2 = _pad2(b2, pad)
    cls = b2.clusters.get(net, {})
    cid = getattr(p2, "cluster", None) if p2 is not None else None
    if island:
        def weight(c):
            return (sum(geom.poly_area(p) for _l, p in cls[c]["islands"]), len(cls[c]["pads"]), -c)
        main = max(sorted(cls), key=weight) if cls else None
        cl = cls.get(main) if main is not None else None
    else:
        cl = cls.get(cid)
    polys = [pts for l, pts in (cl or {}).get("islands", ()) if l == lay]
    if island:                              # the sliver the pad hangs on is not "the pour": only its large pieces are
        polys = [pts for pts in polys if geom.poly_area(pts) >= ISLAND_MIN_AREA]
    shapes = list(pad.shapes.get(lay) or ())
    if not polys or not shapes or lack <= 0:
        return []
    boxes = [geom.poly_bbox(pts) for pts in polys]

    def in_fill(x, y):
        for bb, pts in zip(boxes, polys):
            if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] and geom.pt_in_poly(pts, x, y):
                return True
        return False

    def in_pad(x, y):
        return any(sh.dist_pt(x, y) <= 0.0 for sh in shapes)

    nc = ctx.cfg.nets.get(net)
    bn = ctx.board.nets.get(net)
    w = min(SPOKE_TRACK_W, getattr(nc, "width", None) or getattr(bn, "width", None) or SPOKE_TRACK_W)
    clr = nc.clearance if nc is not None and bn is not None and nc.clearance > bn.clearance + 1e-9 else None
    cands = []
    for k, (ux, uy) in enumerate(_DIRS8):
        edge = 0.0
        while edge < 8.0 and in_pad(pad.x + ux * edge, pad.y + uy * edge):
            edge += 0.05
        if not island and in_fill(pad.x + ux * (edge + SPOKE_TRACK_GAP), pad.y + uy * (edge + SPOKE_TRACK_GAP)):
            continue                        # the pour already comes up to the pad here: that spoke exists
        t = edge + SPOKE_TRACK_GAP
        reach = ISLAND_TRACK_REACH if island else SPOKE_TRACK_REACH
        while t <= edge + reach:
            t += 0.1
            ex, ey = q(pad.x + ux * t), q(pad.y + uy * t)
            if in_fill(ex, ey) and in_fill(pad.x + ux * (t + 0.15), pad.y + uy * (t + 0.15)):
                ex, ey = q(pad.x + ux * (t + 0.15)), q(pad.y + uy * (t + 0.15))
                if ctx.oracle.seg_legal(lay, q(pad.x), q(pad.y), ex, ey, w, net, clearance=clr):
                    cands.append((round(t, 3), k, ex, ey))
                break                       # the first pour along this ray decides the direction
    cands.sort()
    out = []
    for _t, _k, ex, ey in cands[:lack]:
        n = ctx.state.add(net=net, conn=-1, kind="seg", layer=lay, x1=q(pad.x), y1=q(pad.y), x2=ex, y2=ey, w=w,
                          stage="judge")
        out.append(n.id)
    if out or not island:
        return out
    # no straight way to the rest of the pour: one bend (out of the pad between its neighbours, then along the
    # channel), the shortest legal one
    best = None
    o = ctx.oracle
    px, py = q(pad.x), q(pad.y)
    for k, (ux, uy) in enumerate(_DIRS8):
        t1 = 0.4
        while t1 <= 3.2 + 1e-9:
            mx, my = q(pad.x + ux * t1), q(pad.y + uy * t1)
            if not o.seg_legal(lay, px, py, mx, my, w, net, clearance=clr):
                break                       # longer first legs in this direction are blocked too
            for k2, (vx, vy) in enumerate(_DIRS8):
                if k2 == k or ux * vx + uy * vy < -0.1:
                    continue                # same direction (the straight case) or a turn back
                t2 = 0.2
                while t2 <= ISLAND_TRACK_REACH:
                    t2 += 0.2
                    ex, ey = q(mx + vx * t2), q(my + vy * t2)
                    if in_fill(ex, ey) and in_fill(mx + vx * (t2 + 0.15), my + vy * (t2 + 0.15)):
                        ex, ey = q(mx + vx * (t2 + 0.15)), q(my + vy * (t2 + 0.15))
                        cand = (round(t1 + t2, 3), k, k2, mx, my, ex, ey)
                        if (best is None or cand < best) and o.seg_legal(lay, mx, my, ex, ey, w, net, clearance=clr):
                            best = cand
                        break
            t1 += 0.2
    if best is not None:
        _len, _k, _k2, mx, my, ex, ey = best
        for x1, y1, x2, y2 in ((px, py, mx, my), (mx, my, ex, ey)):
            n = ctx.state.add(net=net, conn=-1, kind="seg", layer=lay, x1=x1, y1=y1, x2=x2, y2=y2, w=w, stage="judge")
            out.append(n.id)
    return out


def _thermal_copper(ctx: Ctx, pr, scratch, baseline_raw, chk: dict, connect, budget: Budget,
                    force: bool = False) -> dict:
    """Repair starved thermal reliefs with copper before anything is removed. -> the check to go on with.

    * "zone min spoke count N; actual K": N - K spoke tracks from the pad into its pour (``_spoke_tracks``).
    * "n spokes connected to isolated island": the pour around the pad was cut off from the rest of the net. The pad
      is an open connection in KiCad's ratsnest anyway; the pours search (``connect``) gives it a stub to the net.

    One attempt per pad, and one more each time the pad was given room (the blame rules moved copper out of its
    way: only now can a stub or a spoke track get through). The attempt is kept only when KiCad's next check is better (fewer new errors, then fewer
    open connections); a pad that is still starved afterwards goes through the blame rules as before.
    ``force`` (the pours pass: the board around the pad has changed) tries every starved pad again."""
    j = _jst(ctx)
    tried = j.setdefault("thermal_tried", {})       # pad name -> how often the pad had been given room at that try
    todo = []
    for m in chk["new"]:
        if m.get("type") != "starved_thermal":
            continue
        at = marker_at(m)
        pad = _pad_at(ctx.board, at[0], at[1]) if at else None
        if pad is None:
            continue
        room = len(j["protected"].get(pad.name) or ())
        if tried.get(pad.name, -1) >= room and not force:
            continue                        # tried already, and nothing was moved out of its way since
        todo.append((m, pad))
    if not todo or not chk.get("ex"):
        return chk
    state = ctx.state
    snap = state.snapshot()
    try:
        dru = model.dru_minimums(Path(pr.dir) / f"{pr.name}.kicad_dru") if getattr(pr, "dir", None) else {}
        b2 = model.Board(chk["ex"], dru)
    except Exception:  # noqa: BLE001 — no model of the refilled board: the blame rules apply
        return chk
    added, islands, log = [], set(), []
    for m, pad in todo:
        tried[pad.name] = len(j["protected"].get(pad.name) or ())
        net = _net_name(ctx.board, pad.net)
        msg = str(m.get("message") or m.get("description") or "")
        mt = _SPOKES_RE.search(msg)
        lay = _marker_layer(ctx, m)
        if mt and lay is not None:
            ids = _spoke_tracks(ctx, b2, pad, net, lay, int(mt.group(1)) - int(mt.group(2)))
            added += ids
            log.append({"pad": pad.name, "kind": "spokes", "tracks": len(ids)})
        else:
            ids = _spoke_tracks(ctx, b2, pad, net, lay, 1, island=True) if lay is not None else []
            added += ids
            if not ids:
                islands.add(pad.name)       # no straight way: the pours search looks for a stub
            log.append({"pad": pad.name, "kind": "island", "tracks": len(ids)})
    if islands and connect is not None and not budget.spent_out():
        try:
            ids, _failed = connect(b2, budget, pads=islands)
        except TypeError:
            ids = []
        added += list(ids or [])
    if not added:
        return chk
    chk2 = _check(ctx, pr, scratch, baseline_raw)
    row = {"round": "thermal", "new_errors": len(chk2["new"]), "opens": chk2["score"].opens, "removed": 0,
           "rerouted": 0, "added": len(added)}
    j["rounds"].append(row)
    if chk2["score"] < chk["score"]:
        j.setdefault("thermal_copper", []).extend(log)
        j["repairs"].append(f"{len(todo)} starved thermal relief(s): gave the pad(s) spoke tracks / a stub to the pour")
        return chk2
    state.restore(snap)
    state.new_errors = len(chk["new"])
    row["undone"] = True
    return chk


# ── S7 ────────────────────────────────────────────────────────────────────
def run(ctx: Ctx, budget: Budget, pr, scratch, baseline_raw: list,
        reroute: Callable[[list[int], Budget], None], connect: Callable | None = None) -> StageReport:
    """Commit → refill → DRC → blame → local re-route (≤ 4) → keep best → remove what is still blamed.
    On return ``ctx.state`` and ``scratch.pcb`` are the best round; ``ctx.history['judge']['last']`` is its check."""
    j = _jst(ctx)
    j["baseline_raw"] = baseline_raw
    state = ctx.state
    used0 = budget.used
    rep = StageReport(name="judge")
    best = None
    for rnd in range(ROUNDS + 1):
        _prog(ctx, rnd / (ROUNDS + 2.0), "verifying with KiCad DRC" if rnd == 0 else f"verifying the repair (round {rnd})")
        chk = _check(ctx, pr, scratch, baseline_raw)
        row = {"round": rnd, "new_errors": len(chk["new"]), "opens": chk["score"].opens, "removed": 0, "rerouted": 0}
        j["rounds"].append(row)
        if best is None or chk["score"] < best.score:
            best = _Best(ctx, scratch, chk, rnd)
        if chk["new"] and THERMAL_COPPER:
            chk2 = _thermal_copper(ctx, pr, scratch, baseline_raw, chk, connect, budget)
            if chk2 is not chk:
                chk = chk2
                if chk["score"] < best.score:
                    best = _Best(ctx, scratch, chk, rnd)
        if not chk["new"] or rnd == ROUNDS:
            break
        victims, _unblamed = _blame(ctx, chk, fine=True)
        if not victims:
            break                           # nothing of ours takes part: reported, not repaired
        for m in ctx.history.get("marks") or []:
            m[3] *= HIST_DECAY
        retry, drop = [], []
        j["fresh"] = set()
        for key in sorted(victims, key=str):
            v = victims[key]
            v["again"] = _feedback(ctx, key, v)
            _remove(ctx, key, v)
            if v["again"] and v["conn"] >= 0 and not budget.spent_out():
                retry.append(key)
            else:
                drop.append(key)
        row["removed"] = len(victims)
        for key in drop:
            v = victims[key]
            if v["conn"] < 0:
                note = ""
            elif v["again"]:
                note = "no budget left for a re-route"
            elif v["starved"]:
                note = "the pad was already given room"
            else:
                note = "KiCad rejected every re-route here"
            _mark_removed(ctx, key, v, note)
        if retry:
            _prog(ctx, (rnd + 0.5) / (ROUNDS + 2.0), f"repair: re-routing {len(retry)} connection(s) KiCad rejected")
            reroute(list(retry), budget)
            for key in retry:
                if state.status(key) == "routed":
                    row["rerouted"] += 1
                    j["removed"].pop(key, None)
                else:
                    _mark_removed(ctx, key, victims[key])
    best.restore(ctx, scratch)
    # ── what is still blamed in the best round goes (reported), never ships ──
    for _k in range(REMOVE_ROUNDS):
        if not best.score.new_errors:
            break
        victims, _unblamed = _blame(ctx, best.chk)
        if not victims:
            break
        for key in sorted(victims, key=str):
            _remove(ctx, key, victims[key])
            _mark_removed(ctx, key, victims[key], "still in a DRC error after the repair rounds")
        _prog(ctx, (ROUNDS + 1.0) / (ROUNDS + 2.0), f"removing {len(victims)} connection(s) KiCad still rejects")
        chk = _check(ctx, pr, scratch, baseline_raw)
        j["rounds"].append({"round": "remove", "new_errors": len(chk["new"]), "opens": chk["score"].opens,
                            "removed": len(victims), "rerouted": 0})
        if chk["score"] < best.score:
            best = _Best(ctx, scratch, chk, "remove")
        else:
            best.restore(ctx, scratch)      # the blame was wrong: removing them did not help. Keep the copper.
            rep.notes.append(f"removing {len(victims)} blamed connection(s) did not clear the DRC error(s): kept, "
                             "the residual is reported in drc.new_top")
            break
    j["last"] = best.chk
    if best.score.new_errors:
        _blame(ctx, best.chk)               # refresh j['unblamed'] for the report
    rep.routed, rep.failed = _counts(ctx)
    rep.score = best.score
    rep.work = budget.used - used0
    if best.round not in (j["rounds"][-1]["round"],):
        rep.notes.append(f"kept the result of round {best.round}: later rounds were not better")
    if best.score.new_errors:
        rep.notes.append(f"{best.score.new_errors} new DRC error(s) could not be blamed on new copper (see drc.new_top)")
    rep.data = _data(ctx)
    return rep


def _data(ctx: Ctx) -> dict:
    j = _jst(ctx)
    last = j["last"] or {}
    seen = {}
    for net, why in j["removed"].values():
        seen[(net, why)] = seen.get((net, why), 0) + 1
    return {"new": list(last.get("new") or []), "unconnected": last.get("unconnected"), "stats": last.get("stats") or {},
            "removed_after_drc": [{"net": n, "connections": k, "why": w} for (n, w), k in sorted(seen.items())],
            "rounds": list(j["rounds"]), "commits": j["commits"], "repairs": list(j["repairs"]),
            "unblamed": list(j["unblamed"]), "pour_failed": list(j["pour_failed"]),
            "spoke_blame": list(j.get("spoke_blame") or []), "thermal_copper": list(j.get("thermal_copper") or [])}


# ── pours pass ────────────────────────────────────────────────────────────
def _pour_clusters(ctx: Ctx, b2) -> list[tuple]:
    """[(net, [pad, ...])] one entry per pad cluster of a pour net that the refilled pour does not reach."""
    out = []
    cfg = ctx.cfg
    for name in sorted(b2.clusters):
        net = b2.nets[name]
        cl = b2.clusters[name]
        if not net.has_pour or not is_target(ctx, name) or name not in cfg.nets or len(cl) < 2:
            continue
        from ..guided import geom

        def weight(c):
            return (sum(geom.poly_area(p) for _l, p in cl[c]["islands"]), len(cl[c]["pads"]), -c)
        main = max(sorted(cl), key=weight)
        for c in sorted(cl):
            if c != main and cl[c]["pads"]:
                out.append((name, sorted(cl[c]["pads"], key=lambda p: (p.name, p.x, p.y))))
    return out


def _unreached(clusters: list[tuple]) -> list[dict]:
    """failed[] entries for pour pads nobody could connect (no dog-bone site, no pours search in this build)."""
    out = []
    for net, pads in clusters:
        p = pads[0]
        out.append({"net": net, "from": p.name, "to": "pour", "_a": (p.x, p.y), "_b": None,
                    "where": [round(p.x, 2), round(p.y, 2)],
                    "reason": "the refilled pour does not reach this pad and no stub to it could be placed"})
    return out


def _dogbones(ctx: Ctx, clusters: list[tuple]) -> tuple[list[int], int]:
    """Reserved / new dog-bones for pour pads (W5). -> (piece ids, clusters served)."""
    from . import power
    by_name = {p.name: p for p in ctx.board.pads}
    added, served = [], 0
    for _net, pads in clusters:
        for p2 in pads:
            pad = by_name.get(p2.name)
            if pad is None:
                continue
            try:
                ps = power.place_dogbone(ctx, pad, lanes=False)     # the board is routed: lanes are history
            except TypeError:                                       # (a power module without the keyword)
                ps = power.place_dogbone(ctx, pad)
            if ps:
                added += [p.id for p in ps]
                served += 1
                break
    return added, served


class _Accepted:
    """The last state the pours pass accepted: what a failed attempt goes back to."""

    def __init__(self, ctx, scratch, chk):
        j = _jst(ctx)
        self.snap = ctx.state.snapshot()
        self.chk = chk                      # None: no check yet (pours pass without a judge run)
        self.removed = dict(j["removed"])
        self.pour_pieces = list(j["pour_pieces"])
        self.repairs = list(j["repairs"])
        if chk is not None:
            _copy(_pcb(scratch), j["best_file"])

    @property
    def score(self):
        return self.chk["score"] if self.chk is not None else None

    @property
    def new(self) -> int:
        return len(self.chk["new"]) if self.chk is not None else 0

    def restore(self, ctx, scratch):
        j = _jst(ctx)
        ctx.state.restore(self.snap)
        ctx.state.new_errors = self.new
        j["removed"], j["pour_pieces"], j["repairs"] = dict(self.removed), list(self.pour_pieces), list(self.repairs)
        j["last"] = self.chk
        _copy(j["best_file"] if self.chk is not None else j["base"], _pcb(scratch))


def _nearest_stubs(ctx: Ctx, markers: list, mine: set) -> dict:
    """{key: victim} — for each marker the new pour stub (group) nearest to it, within POUR_BLAME_MM."""
    state = ctx.state
    out = {}
    pieces = [p for p in state.pieces() if p.id in mine]
    for m in markers:
        at = marker_at(m)
        if at is None:
            continue
        best = None
        for p in pieces:
            x, y = _nearest_on(p, at[0], at[1])
            d = math.hypot(x - at[0], y - at[1])
            if d <= POUR_BLAME_MM and (best is None or (d, p.id) < best[:2]):
                best = (d, p.id, p)
        if best is None:
            continue
        p = best[2]
        k = _key(state, p)
        if k not in out:
            out[k] = {"pieces": _group(state, p), "conn": p.conn, "net": p.net, "why": _why(m), "at": at,
                      "type": str(m.get("type")), "starved": None, "blamed_id": p.id}
    return out


def _room_cost(ctx: Ctx, n: int) -> int:
    """Work units a re-route of n connections is expected to need: twice their share of the search pass that
    routed the board (ctx.history['search'] = {work, conns}, set by the engine; 0 = no estimate)."""
    info = ctx.history.get("search") or {}
    if not info.get("work") or not info.get("conns"):
        return 0
    return int(2.0 * n * info["work"] / max(1, info["conns"]))


def _crowd(ctx: Ctx, failed: list[dict]) -> dict:
    """{pad name: (pad, net, rect, [connection idx])}: routed connections of other nets inside the neighbourhood
    of a pour pad the search could not connect (v1: "give the pad room")."""
    j = _jst(ctx)
    out = {}
    pieces = [p for p in ctx.state.pieces() if p.conn >= 0 and not p.protected]
    for e in failed:
        a = e.get("_a")
        pad = _pad_at(ctx.board, a[0], a[1]) if a else None
        if pad is None or pad.name in out:
            continue
        net = _net_name(ctx.board, pad.net)
        rect = _pad_rect(pad)
        if rect in j["protected"].get(pad.name, ()):
            continue
        conns = sorted({p.conn for p in pieces if p.net != net and _in_rect(p, rect)})
        if conns:
            out[pad.name] = (pad, net, rect, conns)
    return out


def open_pour_pads(ctx: Ctx, b2) -> dict:
    """{pad name: (net, key of its cluster)} for every pad of a pour net this run routes that the refilled board
    ``b2`` holds in a cluster other than the net's main one: the pads KiCad's ratsnest still shows as open."""
    out = {}
    for net, pads in _pour_clusters(ctx, b2):
        key = (net, pads[0].name)
        for p in pads:
            out[p.name] = (net, key)
    return out


def _prune_orphans(ctx: Ctx, pr, scratch, baseline_raw, acc, dru, rep: StageReport):
    """Copper of the pours pass that ends up joined to no pad (a stitch via in a pour island that later copper
    reshaped) is taken out again: KiCad lists it as unconnected. Kept only if the board is not worse without it."""
    j = _jst(ctx)
    ex2 = (j["last"] or {}).get("ex")
    if not ex2:
        return acc
    b2 = model.Board(ex2, dru)
    layer_of = {n: i for i, n in enumerate(b2.layers)}

    def r4(v):
        return round(v, 4)
    lone = set()
    for name in sorted(b2.clusters):
        if not is_target(ctx, name):
            continue
        for cid in sorted(b2.clusters[name]):
            c = b2.clusters[name][cid]
            if c["pads"]:
                continue
            for t in c["tracks"]:
                l = t[1] if isinstance(t[1], int) else layer_of.get(t[1], -1)
                lone.add(("s", name, l, r4(t[2]), r4(t[3]), r4(t[4]), r4(t[5])))
                lone.add(("s", name, l, r4(t[4]), r4(t[5]), r4(t[2]), r4(t[3])))
            for v in c["vias"]:
                lone.add(("v", name, r4(v[1]), r4(v[2])))
    if not lone:
        return acc
    gone = []
    for pc in ctx.state.pieces():
        if pc.conn >= 0 or pc.protected:
            continue
        k = ("v", pc.net, r4(pc.x1), r4(pc.y1)) if pc.kind == "via" else \
            ("s", pc.net, pc.layer, r4(pc.x1), r4(pc.y1), r4(pc.x2), r4(pc.y2))
        if k in lone:
            gone.append(pc.id)
    if not gone:
        return acc
    for pid in gone:
        ctx.state.remove(pid)
    chk = _check(ctx, pr, scratch, baseline_raw)
    ok = len(chk["new"]) <= acc.new and not acc.score < chk["score"]
    j["rounds"].append({"round": "prune", "new_errors": len(chk["new"]), "opens": chk["score"].opens,
                        "removed": len(gone), "rerouted": 0, "kept": ok})
    if not ok:
        acc.restore(ctx, scratch)
        return acc
    j["last"] = chk
    rep.notes.append(f"{len(gone)} piece(s) of pour copper that ended up joined to no pad were taken out again")
    return _Accepted(ctx, scratch, chk)


def _rip_phase(ctx: Ctx, pr, scratch, baseline_raw, budget: Budget, acc, rip, dru, rep: StageReport, connect=None):
    """Pour pads no stub and no stitch via can join because routed connections close them in: ``rip(board2, ex2,
    budget)`` proposes, one at a time, a bridge that needs a few routed connections out of its way — it puts the
    bridge into the state and re-routes those connections round it — and KiCad judges each proposal. One that
    leaves the board better (no more errors, fewer open connections) is kept; any other is undone."""
    j = _jst(ctx)
    tries = kept = 0
    again = True
    while again and tries < POUR_RIPS and not budget.spent_out():
        again = False
        ex2 = (j["last"] or {}).get("ex")
        if not ex2:
            break
        b2 = model.Board(ex2, dru)
        if not _pour_clusters(ctx, b2):
            break
        gen = rip(b2, ex2, budget)
        try:
            for prop in gen:
                tries += 1
                chk = _check(ctx, pr, scratch, baseline_raw)
                ok = len(chk["new"]) <= acc.new and chk["score"] < acc.score
                more = []
                if not ok and len(chk["new"]) <= acc.new and connect is not None and not budget.spent_out():
                    # the moved connections may have cut other pour pads off: one plain round for those, then
                    # KiCad once more (the proposal is judged as a whole)
                    more, _f = connect(model.Board(chk["ex"], dru), budget) if chk.get("ex") else ([], [])
                    if more:
                        j["rounds"].append({"round": f"rip{tries}", "new_errors": len(chk["new"]),
                                            "opens": chk["score"].opens, "removed": 0, "rerouted": 0, "kept": False})
                        chk = _check(ctx, pr, scratch, baseline_raw)
                        ok = len(chk["new"]) <= acc.new and chk["score"] < acc.score
                j["rounds"].append({"round": f"rip{tries}", "new_errors": len(chk["new"]), "opens": chk["score"].opens,
                                    "removed": len(prop.get("victims") or ()), "rerouted": len(prop.get("victims") or ()),
                                    "kept": ok})
                if ok:
                    j["last"] = chk
                    j["pour_pieces"] += list(prop.get("added") or ()) + list(more)
                    j["repairs"].append(f"gave room to the {prop.get('net')} pad {prop.get('pad')}")
                    acc = _Accepted(ctx, scratch, chk)
                    kept += 1
                    again = True
                    break
                acc.restore(ctx, scratch)
                if tries >= POUR_RIPS or budget.spent_out():
                    break
        finally:
            gen.close()
    if tries:
        rep.notes.append(f"{kept} of {tries} bridge(s) that needed routed connections moved were accepted by KiCad")
    return acc


def pours_pass(ctx: Ctx, pr, scratch, budget: Budget, connect: Callable | None = None,
               reroute: Callable[[list[int], Budget], None] | None = None, dogbones: bool | None = None,
               rip: Callable | None = None, rip_budget: Budget | None = None) -> StageReport:
    """Pour pads still unconnected after the refill get their reserved dog-bone / a new one
    (``power.place_dogbone``, first round) or a searched stub (``connect``, the engine's pours search): ≤ 2 rounds,
    because a stub can split a pour. A round that adds DRC errors loses the copper KiCad blames; a round that
    does not leave the board better (Score: errors, then KiCad's open connections) is undone.

    When the search cannot reach a pad because this run's copper crowds it, the pad is given room (at most twice):
    its neighbourhood is closed to other nets, the crowding connections are re-routed through ``reroute`` and the
    pours search runs again. The whole attempt is kept only if the board ends up better than before it.

    ``connect(board2, budget) -> (piece ids added to ctx.state, failed dicts in v1's format)``: ``board2`` is
    the ``guided.model.Board`` of the committed, refilled scratch board; the new pieces are ``conn == -1``.
    ``dogbones``: use ``power.place_dogbone`` first (default: when the power module is built; the engine passes
    False when the plan's stages leave 'power' out). Dog-bones that do not help are undone and the search runs."""
    j = _jst(ctx)
    state = ctx.state
    used0 = budget.used
    rep = StageReport(name="pours")
    _ensure_base(ctx, scratch)
    have_power = False
    if dogbones is not False:
        try:
            from . import power
            have_power = not getattr(power, "STUB", False)
        except Exception:  # noqa: BLE001
            have_power = False
    dru = model.dru_minimums(Path(pr.dir) / f"{pr.name}.kicad_dru") if getattr(pr, "dir", None) else {}
    baseline_raw = j.get("baseline_raw") or []
    acc = _Accepted(ctx, scratch, j["last"])
    tentative = False                       # a give-room re-route is in the state and has not paid off yet
    open_pads: list[dict] = []              # pads still unconnected in the accepted state (failed[] entries)
    served = gave = 0
    limit = POUR_ROUNDS + (GIVE_ROOM if reroute is not None else 0)
    for rnd in range(limit):
        ex2 = (j["last"] or {}).get("ex") or kio.extract(scratch, "none", None)
        b2 = model.Board(ex2, dru)
        clusters = _pour_clusters(ctx, b2)
        if not clusters:
            break
        nets = sorted({n for n, _p in clusters})
        _prog(ctx, rnd / float(limit), f"connecting {', '.join(nets[:4])} pads the pour does not reach")
        bones = have_power and rnd == 0
        if bones:
            added, n = _dogbones(ctx, clusters)
            failed = []
        elif connect is not None:
            added, failed = connect(b2, budget)
            n = max(0, len(clusters) - len(failed))
        else:
            added, failed, n = [], [], 0
        j["pour_failed"] = list(failed) if added or failed else _unreached(clusters)
        if not tentative:
            open_pads = list(failed) if failed else _unreached(clusters)
        if added:
            chk = _check(ctx, pr, scratch, baseline_raw)
            row = {"round": f"pours{rnd}", "new_errors": len(chk["new"]), "opens": chk["score"].opens, "removed": 0,
                   "rerouted": 0}
            j["rounds"].append(row)
            if len(chk["new"]) > acc.new and THERMAL_COPPER:
                # a new stub starved a thermal relief next to it: spoke tracks first, removal of the stub second
                chk = _thermal_copper(ctx, pr, scratch, baseline_raw, chk, None, budget, force=True)
            tries = 0
            while len(chk["new"]) > acc.new and tries < POUR_CULPRITS:
                # KiCad rejects some of the new stubs: those go, the rest stay. Blamed by uuid / neighbourhood when
                # a marker names them, else the new stub nearest to the marker
                tries += 1
                mine = set(added)
                victims, _u = _blame(ctx, chk)
                hit = {k: v for k, v in victims.items() if any(p.id in mine for p in v["pieces"])}
                if not hit:
                    hit = _nearest_stubs(ctx, chk["new"], mine)
                if not hit:
                    break
                for key in sorted(hit, key=str):
                    _remove(ctx, key, hit[key])
                    j["removed"][key] = (hit[key]["net"], hit[key]["why"])
                row["removed"] += len(hit)
                added = [i for i in added if state_has(state, i)]
                n = max(0, n - len(hit))
                chk = _check(ctx, pr, scratch, baseline_raw)
                j["rounds"].append({"round": f"pours{rnd}{'bcd'[tries - 1]}", "new_errors": len(chk["new"]),
                                    "opens": chk["score"].opens, "removed": len(hit), "rerouted": 0})
            # useful = no more errors than before and a better score (KiCad sees fewer open connections)
            if len(chk["new"]) > acc.new or (acc.score is not None and not chk["score"] < acc.score):
                acc.restore(ctx, scratch)
                j["pour_failed"] = open_pads if tentative else _unreached(clusters)
                tentative = False
                rep.notes.append(f"pours round {rnd + 1}: the new pad-to-pour copper did not leave the board better "
                                 "in KiCad's DRC and was taken out again")
                if bones and connect is not None:
                    continue                # the dog-bones did not reach the pour: the search gets the next round
                break
            j["last"] = chk
            j["pour_pieces"] += added
            acc = _Accepted(ctx, scratch, chk)
            open_pads = list(failed)
            tentative = False
            served += n
        elif bones and connect is not None:
            continue                        # no dog-bone fits: the search gets the next round
        elif tentative or not failed:
            break                           # the room that was made bought nothing / nothing left to try
        # pads the search could not reach: is this run's own copper in the way? Only once the plain rounds are
        # through (a second round often reaches what the first one's stubs opened up), and only when the budget
        # can finish the re-route (v1: never rip up and then run out)
        plain_done = rnd + 1 >= POUR_ROUNDS or not added
        crowd = _crowd(ctx, failed) if (failed and reroute is not None and gave < GIVE_ROOM and plain_done) else {}
        if crowd and _room_cost(ctx, len({i for _p, _n, _r, conns in crowd.values() for i in conns})) > budget.left():
            rep.notes.append(f"{len(crowd)} pour pad(s) are crowded by routed connections; re-routing those would not "
                             "fit in the budget that is left (raise global.time_budget_s)")
            crowd = {}
        if not crowd or budget.spent_out():
            if not added:
                break
            continue
        gave += 1
        ids = sorted({i for _pad, _net, _rect, conns in crowd.values() for i in conns})
        for name in sorted(crowd):
            pad, net, rect, _conns = crowd[name]
            j["protected"].setdefault(pad.name, []).append(rect)
            _protect_pad(ctx, pad, net, rect, spokes=False)     # the pour does not reach it: there are no spokes yet
            j["repairs"].append(f"gave room to the {net} pad {pad.name} at ({round(pad.x, 2)}, {round(pad.y, 2)})")
        for i in ids:
            state.rip_conn(i)
            state.set_status(i, "todo")
        _prog(ctx, (rnd + 0.5) / float(limit), f"giving {len(crowd)} pour pad(s) room: re-routing {len(ids)} connection(s)")
        reroute(list(ids), budget)
        chk = _check(ctx, pr, scratch, baseline_raw)        # the refill around the re-routed copper
        j["rounds"].append({"round": f"pours{rnd}r", "new_errors": len(chk["new"]), "opens": chk["score"].opens,
                            "removed": len(ids), "rerouted": sum(1 for i in ids if state.status(i) == "routed")})
        if len(chk["new"]) > acc.new:
            # the re-route itself broke rules KiCad checks: stubs laid on top of it cannot leave the board better
            # (measured: three more commit + DRC rounds, all undone) - undo now
            acc.restore(ctx, scratch)
            j["pour_failed"] = open_pads
            rep.notes.append("giving pour pads room made new DRC errors: that re-route was undone")
            break
        j["last"] = chk
        tentative = True
    if tentative:
        acc.restore(ctx, scratch)
        j["pour_failed"] = open_pads
        rep.notes.append("giving pour pads room did not connect more of them: that re-route was undone")
    rb = rip_budget if rip_budget is not None else budget
    if rip is not None and acc.chk is not None and acc.score is not None and not rb.spent_out():
        used1 = rb.used
        acc = _rip_phase(ctx, pr, scratch, baseline_raw, rb, acc, rip, dru, rep, connect)
        if rb is not budget:
            used0 -= rb.used - used1            # the report's work includes the rip phase's own budget
    if acc.chk is not None and (acc.chk.get("orphans") or 0) > 0:
        acc = _prune_orphans(ctx, pr, scratch, baseline_raw, acc, dru, rep)
    # which pads are open in the accepted board: failed[] lists exactly those (one record per cluster)
    ex2 = (j["last"] or {}).get("ex")
    if ex2:
        still = open_pour_pads(ctx, model.Board(ex2, dru))
        j["open_pads"] = still
        seen, keep = set(), []
        for e in j["pour_failed"]:
            k = still.get(e.get("from"))
            if k is not None and k[1] not in seen:
                seen.add(k[1])
                keep.append(e)
        for name in sorted(still):
            net, key = still[name]
            if key not in seen and name == key[1]:
                seen.add(key)
                pad = next((q for q in ctx.board.pads if q.name == name), None)
                keep.append({"net": net, "from": name, "to": "pour", "_a": (pad.x, pad.y) if pad else None, "_b": None,
                             "where": [round(pad.x, 2), round(pad.y, 2)] if pad else None,
                             "reason": "the refilled pour does not reach this pad and no stub to it could be placed"})
        j["pour_failed"] = keep
    rep.routed = served
    rep.failed = len(j["pour_failed"])
    rep.score = (j["last"] or {}).get("score") or state.score()
    rep.work = budget.used - used0
    rep.data = _data(ctx)
    return rep


# ── length tuning, judged (design §14.6 step 4) ───────────────────────────
def _tune_blame(ctx: Ctx, chk: dict, acc) -> dict:
    """{net: [(type, x, y), ...]} — nets whose meanders KiCad blames: the new markers of ``chk`` that the accepted
    board did not have, by the tune pieces they name (uuid, else within ``kio.BLAME_RADIUS``; else the nearest
    tune piece within ``TUNE_BLAME_MM``), plus tuned nets that have more open connections than before."""
    state = ctx.state
    had = {marker_key(m) for m in (acc.chk or {}).get("new") or []}
    out: dict = {}
    tuned = [p for p in state.pieces() if p.stage == "tune"]
    for m in chk["new"]:
        if marker_key(m) in had:
            continue
        at = marker_at(m)
        nets = set()
        for pid in kio.marker_pieces(m, chk["uuids"], chk["pids"], state):
            p = state.piece(pid)
            if p is not None and p.stage == "tune":
                nets.add(p.net)
        if not nets and at is not None:
            best = None
            for p in tuned:
                x, y = _nearest_on(p, at[0], at[1])
                d = math.hypot(x - at[0], y - at[1])
                if d <= TUNE_BLAME_MM and (best is None or (d, p.id) < best[:2]):
                    best = (d, p.id, p.net)
            if best is not None:
                nets.add(best[2])
        for n in sorted(nets):
            out.setdefault(n, []).append((str(m.get("type")), at[0] if at else None, at[1] if at else None))
    unc, unc0 = chk.get("unconnected"), (acc.chk or {}).get("unconnected")
    if isinstance(unc, dict) and isinstance(unc0, dict):
        for n in sorted({p.net for p in tuned}):
            if int(unc.get(n) or 0) > int(unc0.get(n) or 0):
                out.setdefault(n, []).append(("unconnected", None, None))
    return out


_PAIR_RULES = ("diff_pair_gap_out_of_range", "diff_pair_uncoupled_length_too_long")


def _pair_rules_worse(ctx: Ctx, chk: dict, acc, tune) -> dict:
    """{net: marker type} — nets that carry skew bumps of this pass and whose pair KiCad now flags for its gap
    or its uncoupled length although it did not on the accepted board."""
    def flagged(c):
        out = {}
        for n, ts in (((c or {}).get("cons") or {}).get("nets") or {}).items():
            for t in ts:
                if t in _PAIR_RULES:
                    out.setdefault(n, set()).add(t)
        return out
    now, was = flagged(chk), flagged(acc.chk)
    cons = getattr(ctx.x, "cons", None)
    if not now or cons is None or not hasattr(cons, "pair_of"):
        return {}
    edits = (ctx.history.get("tune") or {}).get("edits") or {}
    out = {}
    for n in sorted(now):
        new = now[n] - was.get(n, set())
        pair = cons.pair_of(n)
        if not new or pair is None:
            continue
        for m in (pair.p, pair.n):
            if edits.get(m):
                out[m] = sorted(new)[0]
    return out


def tune_pass(ctx: Ctx, pr, scratch, budget: Budget, tune=None) -> StageReport:
    """Length tuning as a judged pass after ``run`` and ``pours_pass``: the tuner (``v2/tune.py``, or the module
    handed in) adds meanders to the short members of the tuned length groups, KiCad judges the whole board, and
    the result is kept only when it has **no more new DRC errors and no more open connections** than the board
    accepted before the pass. Otherwise the nets whose meanders KiCad blames lose them (``tune.untune`` with the
    marker's neighbourhood closed) and are tuned again elsewhere; a net KiCad rejects in every one of the
    ``TUNE_ROUNDS`` attempts stays as routed and its record says so; a marker nobody can attribute puts the
    accepted board back. A tuned net KiCad still flags as out of range although the router's figure fits is
    tuned again against KiCad's own length (``cal``).

    Nothing is tuned (and KiCad is not asked) on a board without a tuned group, and without a judged check to
    fall back to. ``StageReport.data``: the judge's ``_data`` plus ``tune`` = {'nets', 'groups', 'rounds',
    'rejected'}."""
    j = _jst(ctx)
    used0 = budget.used
    rep = StageReport(name="tune")
    if tune is None:
        from . import tune as tune                  # noqa: PLW0127 — the built module
    cons = getattr(ctx.x, "cons", None)
    skew = cons.skew_pairs() if cons is not None and hasattr(cons, "skew_pairs") else ()
    if j["last"] is None or cons is None or not (cons.tuned() or skew):
        rep.score = (j["last"] or {}).get("score") or ctx.state.score()
        rep.data = _data(ctx)
        return rep
    _ensure_base(ctx, scratch)
    baseline_raw = j.get("baseline_raw") or []
    acc = _Accepted(ctx, scratch, j["last"])
    hist_acc = copy.deepcopy(ctx.history.get("tune"))
    cal: dict = {}
    only = None
    attempts = checks = 0
    dirty = False                                   # the state differs from the accepted board
    rounds = []
    rejected: dict = {}
    data: dict = {}
    while checks < TUNE_ROUNDS + 2:
        changed = False
        if attempts < TUNE_ROUNDS and not budget.spent_out():
            _prog(ctx, attempts / float(TUNE_ROUNDS), "length tuning: adding meanders to the short members")
            r = tune.run(ctx, budget, only=only, cal=cal)
            attempts += 1
            data = r.data or {}
            changed = bool(data.get("changed"))
        if not changed and not dirty:
            break
        chk = _check(ctx, pr, scratch, baseline_raw)
        checks += 1
        row = {"round": f"tune{checks - 1}", "new_errors": len(chk["new"]), "opens": chk["score"].opens,
               "removed": 0, "rerouted": 0}
        j["rounds"].append(row)
        rounds.append(row)
        pair_worse = _pair_rules_worse(ctx, chk, acc, tune)
        if pair_worse:
            # skew bumps that cost the pair its gap / uncoupled-length rule: the bumps go, for good (KiCad's
            # pair rules outrank the skew figure the bumps were laid for; the pair row reports the skew)
            hist = tune._hist(ctx)
            for n, typ in sorted(pair_worse.items()):
                tune.untune(ctx, [n])
                rejected[n] = f"skew bumps undone: with them KiCad reports {typ} for the pair"
                hist["banned"][n] = rejected[n]
            row["removed"] = len(pair_worse)
            only = set(pair_worse)
            dirty = True
            continue
        if len(chk["new"]) > acc.new or (acc.score is not None and chk["score"].opens > acc.score.opens):
            blame = _tune_blame(ctx, chk, acc)
            if not blame:
                # nobody to blame, and some nets carry a stretch on another layer (two vias + copper where the
                # routed board had none: the one thing a meander beside its own run cannot do is cut a pour on
                # another layer off). Those stretches go first — for good — and the nets are tuned again on
                # their own layers; only when that does not help either is the accepted board put back.
                hist = tune._hist(ctx)
                hopped = sorted(n for n, r in hist["nets"].items()
                                if r.get("hops") and n not in (hist.get("nohop") or {}))
                if hopped and attempts < TUNE_ROUNDS:
                    at = marker_at(chk["new"][0]) if chk["new"] else None
                    why = ("; its stretch on another layer was taken out: with it KiCad reported "
                           + (f"{chk['new'][0].get('type')} at ({round(at[0], 2)}, {round(at[1], 2)})" if at
                              else "an open connection on another net"))
                    for n in hopped:
                        tune.untune(ctx, [n])
                        hist.setdefault("nohop", {})[n] = why
                    row["removed"] = len(hopped)
                    only = set(hopped)
                    dirty = True
                    continue
                rep.notes.append("length tuning: KiCad reported a new error that no meander could be blamed for; "
                                 "the board accepted before tuning was put back")
                at = marker_at(chk["new"][0]) if chk["new"] else None
                for n, rec in sorted((data.get("nets") or {}).items()):
                    if rec.get("bumps"):
                        rejected.setdefault(n, "meanders undone: KiCad reported a new error no single meander could "
                                               "be blamed for" + (f" (first at ({round(at[0], 2)}, {round(at[1], 2)}))"
                                                                  if at else ""))
                dirty = True
                break
            hist = tune._hist(ctx)
            for n in sorted(blame):
                typ, x, y = blame[n][0]
                rects = [(mx - TUNE_AVOID_MM, my - TUNE_AVOID_MM, mx + TUNE_AVOID_MM, my + TUNE_AVOID_MM)
                         for _t, mx, my in blame[n] if mx is not None]
                tune.untune(ctx, [n], avoid=rects)
                where = f" at ({round(x, 2)}, {round(y, 2)})" if x is not None else ""
                rejected[n] = f"meander rejected by KiCad{where}: {typ}"
                if attempts >= TUNE_ROUNDS:
                    hist["banned"][n] = rejected[n] + f" (in each of {TUNE_ROUNDS} attempts)"
            row["removed"] = len(blame)
            only = set(blame)
            dirty = True
            continue
        j["last"] = chk
        acc = _Accepted(ctx, scratch, chk)
        hist_acc = copy.deepcopy(ctx.history.get("tune"))
        dirty = False
        # KiCad's own figure for the nets it still flags: tune those again against it
        klen = (chk.get("cons") or {}).get("len") or {}
        flagged = (chk.get("cons") or {}).get("nets") or {}
        miss = {}
        for n, rec in sorted((data.get("nets") or {}).items()):
            if rec.get("bumps") and "short_by" not in rec and n in klen and any(
                    t in ("skew_out_of_range", "length_out_of_range") for t in flagged.get(n) or ()):
                d = klen[n] - (float(rec["after"]) - float(cal.get(n, 0.0)))
                if abs(d) > 1e-5:
                    miss[n] = d
        if not miss or attempts >= TUNE_ROUNDS:
            break
        cal.update(miss)
        only = set(miss)
        rep.notes.append("length tuning: KiCad measures " + ", ".join(
            f"{n} {miss[n]:+.4f} mm" for n in sorted(miss)[:4]) + " against the router's figure; tuned again to KiCad's")
    if dirty:
        acc.restore(ctx, scratch)
        if hist_acc is None:
            ctx.history.pop("tune", None)
        else:
            ctx.history["tune"] = hist_acc
        for n, why in sorted(rejected.items()):     # the reason survives the restore
            rec = tune._hist(ctx)["nets"].get(n)
            if rec is not None and not rec.get("bumps"):
                rec.setdefault("why", why)
    hist = tune._hist(ctx)
    lost = sorted(n for n in rejected if not (hist["nets"].get(n) or {}).get("bumps"))
    if lost:
        # nets that ended without meanders (given up, or back on the accepted board): their record — how short,
        # and why — is written by the tuner itself; a net it gave up on gets no copper from this call
        gave_up = dict(hist["banned"])
        for n in lost:
            hist["banned"].setdefault(n, rejected[n])
        tune.run(ctx, Budget(0), only=set(lost), cal=cal)
        hist["banned"] = gave_up
    for n, why in sorted(rejected.items()):
        rec = hist["nets"].get(n)
        if rec is not None and "short_by" in rec and not rec.get("bumps"):
            rec["why"] = hist["banned"].get(n) or why
    nets = {n: dict(r) for n, r in sorted(hist["nets"].items())}
    laid = [r for r in nets.values() if r.get("bumps")]
    if laid:
        # (session.compare scores length as it always did: say how much of it is wanted)
        rep.notes.append(f"length tuning: {len(laid)} net(s) got meanders, "
                         f"{round(sum(float(r.get('added') or 0.0) for r in laid), 2)} mm of the added length")
        hopped = sorted(n for n, r in nets.items() if r.get("bumps") is not None and r.get("hops"))
        if hopped:
            rep.notes.append(
                f"length tuning: {len(hopped)} net(s) had no room beside them on their own layer and carry a "
                "stretch on another layer through two vias each: " + ", ".join(
                    f"{n} ({'/'.join(nets[n].get('hop_layers') or [])})" for n in hopped[:4])
                + (f" (+{len(hopped) - 4} more)" if len(hopped) > 4 else "")
                + "; nets[..].max_vias: 0 keeps a net on its layer")
    rep.routed = sum(1 for r in nets.values() if r.get("bumps") and "short_by" not in r)
    rep.failed = sum(1 for r in nets.values() if "short_by" in r or "long_by" in r)
    rep.score = (j["last"] or {}).get("score") or ctx.state.score()
    rep.work = budget.used - used0
    rep.data = _data(ctx)
    rep.data["tune"] = {"nets": nets, "rounds": rounds, "rejected": dict(sorted(rejected.items())),
                        "groups": data.get("groups") or {}, "cal": {n: round(v, 4) for n, v in sorted(cal.items())}}
    return rep


def state_has(state, pid: int) -> bool:
    f = getattr(state, "piece", None)
    if callable(f):
        return f(pid) is not None
    return any(p.id == pid for p in state.pieces())
