"""S5 repair: the bounded stuck ladder (design §4.9).

For the failed connections, in ``rank`` order, inside transactions and inside the stage budget. One rung is tried
on every failed connection before the next rung starts (a connection that needs the expensive rungs cannot starve
one a cheap rung repairs):

  probe         exact ray probe of both terminals (is a pad boxed in, by what, how much of it is fixed copper)
  rescue        one extra via (also outside the layer assignment of the global stage); half window pitch; neck
                width; the other fan-out side (only this connection's own escape moves). Disturbs nobody, so it
                runs before the rips (``RESCUE_FIRST``; the design lists it after them — measured, see
                docs/internal/v2-interface-notes.md, W7).
  min-cut rip   soft search (``detail.route_conn(soft_cost=...)``) names the cheapest set of routed connections
                in the way; exactly those are ripped, the failed one is routed, the victims are re-routed.
                Accepted only when the score improved and every victim is routed again; else rolled back, the
                pair goes on the tabu list and the victims get 0.3 mm of history.
  permutation   the failed connection and the connections contesting its channel (<= 5, 12 x 12 mm box) are
                all ripped and tried in every order (lexicographic, <= 120, shared prefixes routed once).
  feedback      capacity of the tile borders at the failure is booked away, S3 and S4 re-run for the
                connections crossing them (at most 2 loops per board).
  certificate   the ``failed[]`` record: reason, cut (named, capacity vs demand), blocked_by, static_share,
                mincut_nets, probe, tried, suggest.

Everything goes through the sibling interfaces of design §9 (``pieces.RouteState``, ``detail.route_conn``,
``fanout.re_escape`` / ``registry`` / ``sync``, ``power.place_dogbone``, ``tiles.Tiles``, ``globalroute.run``); a
sibling that is still a stub switches its rung off. ``DEPS`` lets the engine or a test inject a replacement.
What every rung did is in ``StageReport.data['log']``.

No clock, no randomness, no iteration over unordered containers: same state in, same copper out.
"""
from __future__ import annotations

import dataclasses
import importlib
import math
import os

from .types import (PROTECT_FACTOR, CHARGE_SEARCH, ORACLE_CHECKS_PER_UNIT, Blocker, Budget, ConnV2, Ctx, Cut,  # noqa: F401
                    Piece, Score, StageReport, q)

BASE_RIP_MM = 4.0          # price of ripping one connection of minimum width that runs straight
HISTORY_MM = 0.3           # added to a victim (and its contested spot) each time a rip of it did not pay
HISTORY_RADIUS_MM = 0.5
HISTORY_POINTS = 8         # contested spots written per rip that did not pay
MAX_RIP_ATTEMPTS = 3       # soft searches per connection and ladder
CLUSTER_BOX_MM = 12.0
CLUSTER_MAX = 5
PERM_MAX = 120             # orders tried per cluster
BRIDGE_RUNG = True         # a re-route that fails pad to pad is tried as a bridge between the two parts of its tree
LAZY_CASCADE = True        # connections that hang on ripped copper are re-routed after the rip's re-routes, and only
                           # when they are still apart then (v1-made trees join through each other's copper: ripping
                           # every connection whose terminals a victim joined would take half a power net along)
VICTIM_FINE = False        # a ripped victim that finds no other way: also the lattice at half / quarter pitch. Off:
                           # measured, it buys nothing on the corpus and its work costs a budget-bound board 2 connections
PERM_FINE = False          # ... the same for a member of a permuted cluster (off for the same reason)
TAIL_MAX = 0               # eager cascade only: tee-dependents a cluster has no room for, re-routed as a tail after
                           # each order (0 = the cluster is given up, as designed; 8 measured: one connection lost on a
                           # 2-layer board)
FEEDBACK_LOOPS = 2         # per board
FEEDBACK_MAX_CONNS = 12    # connections re-globalled per loop
FEEDBACK_SHARE = 0.2       # of the stage budget, kept back for the feedback loops
CUT_REACH_MM = CLUSTER_BOX_MM / 2
HARD_RULES = frozenset(("clearance", "hole", "edge", "keepout", "mask", "short"))
RESCUE_FIRST = True        # ladder order: rescue (disturbs nobody) before min-cut rip and permutation
LOG_MAX = 400
LADDER = ("route", "probe", "mincut", "permute", "rescue", "feedback")

DEPS: dict = {}            # {'detail'|'fanout'|'power'|'globalroute': module-like}: overrides the sibling module


def _env_switches() -> None:
    """COPPER_V2_REPAIR='NAME=0,NAME=1,...' sets this module's boolean switches (A/B measurements on the bench)."""
    for item in (os.environ.get("COPPER_V2_REPAIR") or "").split(","):
        k, _, v = item.strip().partition("=")
        if k in ("VICTIM_FINE", "PERM_FINE", "BRIDGE_RUNG", "LAZY_CASCADE", "RESCUE_FIRST") and v in ("0", "1"):
            globals()[k] = v == "1"


_env_switches()


def _dep(name: str):
    """The sibling module, or None while it is a stub / not importable (its rung is then skipped)."""
    if name in DEPS:
        return DEPS[name]
    try:
        m = importlib.import_module(f"{__package__}.{name}")
    except Exception:  # noqa: BLE001
        return None
    return None if getattr(m, "STUB", False) else m


# ── memory that survives between calls on one board (ctx.history['repair']) ──────────────────────────
class Memory:
    """Tabu list, rip history and what each ladder found. One per Ctx, kept in ``ctx.history['repair']``."""

    def __init__(self):
        self.tabu: set = set()            # (conn, frozenset(victims)) and ('perm', frozenset(cluster))
        self.blame: dict = {}             # tabu key -> victims that could not be re-routed
        self.rip_hist: dict = {}          # victim conn (or ('res', net)) -> mm
        self.ripped: dict = {}            # conn -> times ripped as a victim
        self.mincut: dict = {}            # conn -> [victim conn, ...] in discovery order
        self.contested: dict = {}         # conn -> [conn, ...] second-level blockers (what the victims fight)
        self.spots: dict = {}             # conn -> [(x, y, layer), ...] where it crossed its victims
        self.tried: dict = {}             # conn -> [ladder step, ...]
        self.probe: dict = {}             # conn -> probe()
        self.channel: dict = {}           # conn -> {'conns': [...], 'nets': [...]} exhausted permutation
        self.static_cut: set = set()      # conns with no path even through every rippable piece
        self.starved: set = set()         # conns whose ladder was cut short by the budget
        self.fails: dict = {}             # conn -> last hard-search fail record (when the state keeps none)
        self.feedback_loops = 0
        self.piece_info = None            # does detail report crossed piece ids? None = not known yet
        self.stats = {"route_calls": 0, "soft_searches": 0, "rips": 0, "accepted": 0, "rollbacks": 0,
                      "tabu_hits": 0, "perm_orders": 0, "perm_solved": 0, "rescued": 0, "feedback_gain": 0}
        self.log: list = []               # what each rung did, newest last (bounded; StageReport.data['log'])
        self._conns = None
        self._map: dict = {}
        self._pads = None
        self._wmin = 0.0

    def conn(self, ctx: Ctx, idx: int):
        if self._conns is not ctx.conns or len(self._map) != len(ctx.conns):
            self._conns = ctx.conns
            self._map = {c.idx: c for c in ctx.conns}
            ws = [c.width for c in ctx.conns if c.width > 0]
            self._wmin = min(ws) if ws else 0.0
        return self._map.get(idx)

    def wmin(self, ctx: Ctx) -> float:
        self.conn(ctx, -1)
        if self._wmin > 0:
            return self._wmin
        rules = getattr(ctx.board, "rules", None) or {}
        return rules.get("track_width") or 0.2

    def pad(self, ctx: Ctx, name):
        if self._pads is None:
            self._pads = {p.name: p for p in getattr(ctx.board, "pads", ())}
        return self._pads.get(name) if name else None

    def event(self, kind: str, conn: int, **kw) -> None:
        if len(self.log) < LOG_MAX:
            e = {"step": kind, "conn": conn}
            e.update(kw)
            self.log.append(e)
            if os.environ.get("COPPER_V2_DEBUG"):
                import sys
                print("[v2 repair]", e, file=sys.stderr, flush=True)

    def note(self, idx: int, step: str) -> None:
        t = self.tried.setdefault(idx, [])
        if step not in t:
            t.append(step)

    def push(self, table: dict, idx: int, items) -> None:
        lst = table.setdefault(idx, [])
        for it in items:
            if it not in lst:
                lst.append(it)


def memory(ctx: Ctx) -> Memory:
    m = ctx.history.get("repair")
    if not isinstance(m, Memory):
        m = ctx.history["repair"] = Memory()
    return m


# ── small helpers over the sibling interfaces ───────────────────────────────────────────────────────
def _rank(c: ConnV2):
    return (c.rank, c.idx)


def _fail_of(ctx: Ctx, idx: int):
    """The fail record of a connection: RouteState has no getter in design §9, so every likely spelling is tried."""
    st = ctx.state
    f = getattr(st, "fail", None)
    if callable(f):
        try:
            r = f(idx)
            if r:
                return r
        except Exception:  # noqa: BLE001
            pass
    for name in ("fails", "fail_info", "failed"):
        d = getattr(st, name, None)
        if isinstance(d, dict) and d.get(idx):
            return d[idx]
    return memory(ctx).fails.get(idx)


def _set_failed(ctx: Ctx, idx: int, fail: dict | None) -> None:
    ctx.state.set_status(idx, "failed", fail)
    if fail is not None:
        memory(ctx).fails[idx] = fail


def _save(ctx: Ctx, idxs, into: dict | None = None) -> dict:
    """Status, fail record and terminals of connections about to be touched (rollback() need not restore them)."""
    mem = memory(ctx)
    into = {} if into is None else into
    for i in idxs:
        if i in into:
            continue
        c = mem.conn(ctx, i)
        if c is None:
            continue
        into[i] = (ctx.state.status(i), _fail_of(ctx, i), c.a, c.b, ctx.corridors.get(i))
    return into


def _restore(ctx: Ctx, saved: dict) -> None:
    mem = memory(ctx)
    for i in sorted(saved):
        st, fail, a, b, cor = saved[i]
        c = mem.conn(ctx, i)
        c.a, c.b = a, b
        if cor is None:
            ctx.corridors.pop(i, None)
        elif ctx.corridors.get(i) is not cor:
            ctx.corridors[i] = cor
        if st == "failed":
            _set_failed(ctx, i, fail)
        elif ctx.state.status(i) != st:
            ctx.state.set_status(i, st)


def _route(ctx: Ctx, conn: ConnV2, budget: Budget, *, record: bool = True, **kw) -> dict:
    """detail.route_conn, normalised. A hard success marks the connection routed."""
    mem = memory(ctx)
    d = _dep("detail")
    if d is None:
        return {"ok": False, "pieces": [], "fail": {"reason": "detail stage unavailable"}, "crossed": {}}
    mem.stats["route_calls"] += 1
    r = dict(d.route_conn(ctx, conn, budget, **kw) or {})
    r["ok"] = bool(r.get("ok"))
    r["crossed"] = {int(k): v for k, v in (r.get("crossed") or {}).items()}
    r.setdefault("pieces", [])
    if r["ok"] and not r["crossed"]:
        ctx.state.set_status(conn.idx, "routed")
    elif record and not kw.get("soft_cost") and r.get("fail"):
        mem.fails[conn.idx] = r["fail"]
    return r


def _free_layers(ctx: Ctx, conn: ConnV2) -> bool:
    """Lift the layer assignment the global stage gave this connection (its corridor keeps its tiles): the detail
    search may then change layer wherever the plan allows. True when there was something to lift. Callers put the
    old corridor back when the route fails (``_save`` / ``_restore`` do)."""
    cor = ctx.corridors.get(conn.idx)
    if cor is None or not (cor.layers or cor.layer_seq):
        return False
    ctx.corridors[conn.idx] = dataclasses.replace(cor, layers=(), layer_seq=())
    return True


def _reroute(ctx: Ctx, conn: ConnV2, budget: Budget) -> dict:
    """Re-route a ripped connection: S4 steps 1-6 as they are; when that fails, once more in the corridor + 2 rings
    with the global layer assignment lifted and one extra via."""
    r = _route(ctx, conn, budget, record=False)
    if r["ok"] or budget.spent_out():
        return r
    cor = ctx.corridors.get(conn.idx)
    if not _free_layers(ctx, conn):
        return r
    r2 = _route(ctx, conn, budget, record=False, widen=2, extra_vias=1)
    if r2["ok"]:
        return r2
    ctx.corridors[conn.idx] = cor
    return r


def _bridge(ctx: Ctx, conn: ConnV2, budget: Budget) -> dict | None:
    """The two parts of the net this connection is there to join, bridged wherever they come nearest: from ANY
    copper of terminal a's part (pads, tracks, vias) to ANY copper of terminal b's. The connection's own pad can
    be closed in by its net's other tracks while the tree it hangs on has a free way two pads further on (trees
    laid by the v1 search join clusters, not the two named pads). None = not applicable."""
    if not _bridging(ctx) or pieces_open(conn.b) or not conn.a.cluster or not conn.b.cluster:
        return None
    try:
        from . import engine
        spec = engine._bridge_spec(ctx.board, ctx.state, conn, to=conn.b.cluster)
    except Exception:  # noqa: BLE001 — a state / board without what the spec needs: the rung is skipped
        return None
    if spec is None:
        return None
    memory(ctx).stats["bridges"] = memory(ctx).stats.get("bridges", 0) + 1
    r = _route(ctx, conn, budget, record=False, bridge=spec, extra_vias=1)
    if not r["ok"] and not budget.spent_out():
        r2 = _route(ctx, conn, budget, record=False, bridge=spec, extra_vias=1, pitch_scale=0.5)
        if r2["ok"]:
            return r2
    return r


def pieces_open(t) -> bool:
    return not getattr(t, "pad", None) and not getattr(t, "cluster", None) and not getattr(t, "layers", None)


def _last_resort(ctx: Ctx, conn: ConnV2, budget: Budget, fine: bool) -> dict | None:
    """What a ripped connection (a victim, a member of a permuted cluster) gets when its plain re-route fails:
    the bridge between the two parts of its tree, then — ``fine`` — the lattice at half and at a quarter of the
    window pitch (a gap that holds exactly one track, between the two pads of a chip part, say, whose centre line
    the lattice misses). None = nothing applied."""
    r = _bridge(ctx, conn, budget)
    if r is not None and (r["ok"] or budget.spent_out()):
        return r
    if not fine or budget.spent_out():
        return r
    memory(ctx).stats["fine_reroutes"] = memory(ctx).stats.get("fine_reroutes", 0) + 1
    r = _route(ctx, conn, budget, record=False, widen=2, pitch_scale=0.5, extra_vias=1)
    if r["ok"] or budget.spent_out():
        return r
    return _route(ctx, conn, budget, record=False, widen=2, pitch_scale=0.25, extra_vias=1)


def _rip(ctx: Ctx, idx: int, also=frozenset()) -> list:
    """Remove the unprotected pieces of a connection (escape stubs stay unless named in ``also``)."""
    out = []
    for p in sorted(ctx.state.pieces(conn=idx), key=lambda p: p.id):
        if p.protected and p.id not in also:
            continue
        ctx.state.remove(p.id)
        out.append(p)
    ctx.state.set_status(idx, "todo")
    return out


def _net_protected(ctx: Ctx, net: str) -> bool:
    x = ctx.x
    if PAIR_PROTECT and net in _coupled_nets(ctx):
        return True                    # the lanes of a coupled pair are one object: never ripped for another net
    if x is None:
        return False
    f = getattr(x, "net", None)
    if callable(f):
        try:
            return bool(getattr(f(net), "protect", False))
        except Exception:  # noqa: BLE001
            return False
    return net in (getattr(x, "protect", None) or ())


PAIR_PROTECT = os.environ.get("COPPER_V2_PAIR_PROTECT", "1") != "0"     # measurement switch


def _coupled_nets(ctx: Ctx) -> set:
    """Nets that carry coupled differential-pair lanes (``pairs.coupled_nets``); empty on every board without."""
    h = ctx.history.get("pairs") if isinstance(getattr(ctx, "history", None), dict) else None
    if not h:
        return set()
    try:
        from . import pairs
        return pairs.coupled_nets(ctx)
    except Exception:  # noqa: BLE001
        return set()


def _clearance(ctx: Ctx, conn: ConnV2) -> float:
    if conn.clearance > 0:
        return conn.clearance
    try:
        return ctx.oracle.clearance(conn.net)
    except Exception:  # noqa: BLE001
        return 0.2


def _charge_checks(ctx: Ctx, budget: Budget | None, before: int) -> None:
    if budget is not None:
        budget.charge((ctx.oracle.checks - before) // ORACLE_CHECKS_PER_UNIT)


def _unrouted(ctx: Ctx, idxs) -> int:
    return sum(1 for i in idxs if ctx.state.status(i) != "routed")


def _v1_trees(ctx: Ctx) -> bool:
    """Does this run work on trees the v1 search laid (``x.v1_trees``: the engine's second opinion)? There a
    connection's copper joins clusters, not the two pads it names, and the two rules below apply. On trees the
    window search laid they do not pay (measured: 2 connections lost on one 6-layer board, 1 on two others)."""
    return bool(getattr(ctx.x, "v1_trees", False)) or os.environ.get("COPPER_V2_V1_TREES") == "1"


def _lazy(ctx: Ctx) -> bool:
    return LAZY_CASCADE and _v1_trees(ctx)


def _bridging(ctx: Ctx) -> bool:
    return BRIDGE_RUNG and _v1_trees(ctx)


def _still_connected(ctx: Ctx, idx: int) -> bool:
    """Is a routed connection still joined after a rip? By the strict rule, or by KiCad's (an end inside the other
    piece's copper): copper laid by the v1 search joins that way, and a connection that never touched the ripped
    copper must not be dragged along."""
    if ctx.state.connected(idx):
        return True
    if not _v1_trees(ctx):
        return False
    try:
        return bool(ctx.state.connected(idx, loose=True))
    except TypeError:                                    # a state without the loose rule
        return False


def _cascade(ctx: Ctx, ripped: list[int], saved: dict) -> list[int]:
    """Routed connections that teed onto copper just ripped are no longer connected: rip them as well."""
    mem = memory(ctx)
    extra: list[int] = []
    nets = {mem.conn(ctx, i).net for i in ripped if mem.conn(ctx, i) is not None}
    changed = True
    while changed:
        changed = False
        for c in sorted(ctx.conns, key=_rank):
            if c.net not in nets or c.idx in ripped or c.idx in extra or ctx.state.status(c.idx) != "routed":
                continue
            if not _still_connected(ctx, c.idx):
                _save(ctx, [c.idx], saved)
                _rip(ctx, c.idx)
                extra.append(c.idx)
                changed = True
    return extra


# ── rip price ───────────────────────────────────────────────────────────────────────────────────────
def rip_price(ctx: Ctx, victim: int, piece: Piece | None = None) -> float:
    """mm of detour the failed connection may spend to avoid ripping ``victim`` (design §4.9 step 1):
    ``4 mm x (w / w_min) x (8 if protected) / max(1, routed_len / est) + history``.
    ``piece``: price this piece of the victim (its ``protected`` flag counts); needed for reservations
    (``victim < 0``), which have no connection."""
    mem = memory(ctx)
    c = mem.conn(ctx, victim) if victim >= 0 else None
    wmin = mem.wmin(ctx)
    if c is not None:
        w = c.width or wmin
        ratio = 1.0
        if c.est > 0:
            ln = sum(p.length for p in ctx.state.pieces(conn=victim))
            ratio = max(1.0, ln / c.est)
        hist = mem.rip_hist.get(victim, 0.0)
    else:
        w = piece.w if piece is not None and piece.kind == "seg" else wmin
        ratio = 1.0
        hist = mem.rip_hist.get(("res", piece.net if piece is not None else ""), 0.0)
    price = BASE_RIP_MM * max(1.0, w / wmin)
    if piece is not None and piece.protected:
        price *= PROTECT_FACTOR
    return price / ratio + hist


def _soft_table(ctx: Ctx, conn: ConnV2, protected_ok: bool, hard: frozenset = frozenset()) -> dict:
    """piece id -> rip price for every piece the failed connection may cross. Pieces left out stay hard:
    its own net, plan-protected nets, reservations nobody can re-place, and the connections in ``hard``."""
    prices: dict = {}
    table: dict = {}
    power = _dep("power")
    fan = _dep("fanout")
    prot: dict = {}
    for p in ctx.state.pieces():
        if p.net == conn.net or p.conn in hard:
            continue
        if p.net not in prot:
            prot[p.net] = _net_protected(ctx, p.net)
        if prot[p.net]:
            continue
        if p.protected:
            if not protected_ok:
                continue
            if p.stage == "power":
                if p.conn >= 0 or power is None:
                    continue
            elif p.stage != "fanout" or fan is None:
                continue
        elif p.conn < 0:
            continue
        if p.conn < 0:
            table[p.id] = rip_price(ctx, -1, p)
            continue
        base = prices.get(p.conn)
        if base is None:
            base = prices[p.conn] = rip_price(ctx, p.conn)
        table[p.id] = base * PROTECT_FACTOR if p.protected else base
    return table


def _bump(ctx: Ctx, conn: ConnV2, victims, pids=None) -> None:
    """History: 0.3 mm on every victim of a rip that did not pay, and on the cells where they were crossed
    (``ctx.history['points']``: (x, y, radius, mm, layer), the list window.build prices every later search with)."""
    mem = memory(ctx)
    for v in victims:
        mem.rip_hist[v] = mem.rip_hist.get(v, 0.0) + HISTORY_MM
    pts = ctx.history.setdefault("points", [])
    if isinstance(pts, list):
        for x, y, layer in mem.spots.get(conn.idx, ())[:HISTORY_POINTS]:
            pts.append((q(x), q(y), HISTORY_RADIUS_MM, HISTORY_MM, layer) if layer >= 0
                       else (q(x), q(y), HISTORY_RADIUS_MM, HISTORY_MM))


# ── step 0: probe ───────────────────────────────────────────────────────────────────────────────────
_DIRS16 = tuple((round(math.cos(math.pi * k / 8), 12), round(math.sin(math.pi * k / 8), 12)) for k in range(16))


def _pad_radius(ctx: Ctx, t) -> float:
    pad = memory(ctx).pad(ctx, t.pad)
    if pad is None or math.hypot(pad.x - t.x, pad.y - t.y) > 1e-4:
        return 0.0
    r = 0.0
    for layer in t.layers:
        for s in pad.shapes.get(layer, ()):
            b = s.bbox
            r = max(r, math.hypot(b[0] - pad.x, b[1] - pad.y), math.hypot(b[2] - pad.x, b[3] - pad.y))
    return r


def _rank_blockers(hits: list) -> list[dict]:
    groups: dict = {}
    for b in hits:
        key = (b.tag or "?") if b.static else (b.net or "?")
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"name": key, "net": b.net, "tag": b.tag, "rule": b.rule, "static": b.static, "count": 0,
                               "dist": b.dist, "need": b.need}
        g["count"] += 1
        if b.dist < g["dist"]:
            g["dist"], g["need"], g["rule"] = b.dist, b.need, b.rule
    return sorted(groups.values(), key=lambda g: (-g["count"], g["dist"], g["name"]))


def probe(ctx: Ctx, conn: ConnV2, budget: Budget | None = None) -> dict:
    """Exact ray probe of both terminals at neck width: 16 straight exits per layer, each long enough to clear the
    pad by two track pitches. A terminal with no legal exit is reported as enclosed.
    -> {enclosed: 'a'|'b'|'none', ends: {a|b: {open, blocked}}, static_share, blockers: [ranked]}
    ("enclosed" means no straight exit; a search may still find a bent one, so repair only uses it to explain)."""
    o = ctx.oracle
    c0 = o.checks
    w = conn.neck if 0 < conn.neck < conn.width else (conn.width or memory(ctx).wmin(ctx))
    cl = _clearance(ctx, conn)
    clr = conn.clearance if conn.clearance > 0 else None
    out = {"enclosed": "none", "ends": {}, "static_share": 0.0, "blockers": []}
    hits: list = []
    for end, t in (("a", conn.a), ("b", conn.b)):
        if not t.layers:                                 # an open target (a pour): nothing to probe
            out["ends"][end] = {"open": True, "blocked": 0, "at": [t.x, t.y], "pad": t.pad}
            continue
        reach = _pad_radius(ctx, t) + 2.0 * (w + cl)
        is_open = False
        mine: list = []
        for layer in t.layers:
            for dx, dy in _DIRS16:
                x2, y2 = q(t.x + dx * reach), q(t.y + dy * reach)
                if o.seg_legal(layer, t.x, t.y, x2, y2, w, conn.net, clr):
                    is_open = True
                    break
                for b in o.blockers(layer, t.x, t.y, x2, y2, w, conn.net, limit=4, clearance=clr):
                    if b.rule in HARD_RULES:
                        mine.append(b)
                        break
            if is_open:
                break
        out["ends"][end] = {"open": is_open, "blocked": len(mine), "at": [t.x, t.y], "pad": t.pad}
        if not is_open:
            hits += mine
            if out["enclosed"] == "none":
                out["enclosed"] = end
    if hits:
        out["static_share"] = round(sum(1 for b in hits if b.static) / len(hits), 3)
        out["blockers"] = _rank_blockers(hits)
    _charge_checks(ctx, budget, c0)
    return out


# ── step 1: min-cut rip ─────────────────────────────────────────────────────────────────────────────
def _piece_map(ctx: Ctx) -> dict:
    return {p.id: p for p in ctx.state.pieces()}


def _dogbone_group(pmap: dict, p: Piece) -> list:
    """The protected power pieces that form one dog-bone with ``p`` (stub + via, joined end to end)."""
    pool = [x for x in pmap.values() if x.conn < 0 and x.protected and x.stage == "power" and x.net == p.net]
    group = {p.id: p}
    grew = True
    while grew:
        grew = False
        for x in pool:
            if x.id in group:
                continue
            for g in list(group.values()):
                ends_g = ((g.x1, g.y1), (g.x2, g.y2))
                ends_x = ((x.x1, x.y1), (x.x2, x.y2))
                if any(abs(a[0] - b[0]) < 2e-6 and abs(a[1] - b[1]) < 2e-6 for a in ends_g for b in ends_x):
                    group[x.id] = x
                    grew = True
                    break
    return [group[i] for i in sorted(group)]


def _dogbone_of(ctx: Ctx, pmap: dict, p: Piece):
    """(pad, pieces) of the dog-bone ``p`` belongs to: from power's book (``ctx.history['power']['dogbones']``)
    when it lists the piece, else from the geometry."""
    book = (ctx.history.get("power") or {}).get("dogbones") if isinstance(ctx.history.get("power"), dict) else None
    for name in sorted(book or ()):
        rec = book[name]
        ids = (rec.get("pieces") or ()) if isinstance(rec, dict) else ()
        if p.id in ids:
            pad = memory(ctx).pad(ctx, name)
            group = [pmap[i] for i in sorted(ids) if i in pmap]
            if pad is not None and group:
                return pad, group
    group = _dogbone_group(pmap, p)
    return _dogbone_pad(ctx, group), group


def _escape_users(ctx: Ctx, pmap: dict, p: Piece):
    """([(conn idx, end), ...], [piece ids]) of the escape stub ``p`` is part of: from fanout's book when it has one
    (its stubs are ``conn = -1`` pieces shared by every connection of the pad), else the stub's own connection."""
    fan = _dep("fanout")
    reg = getattr(fan, "registry", None)
    if callable(reg):
        by_pad = reg(ctx).get("by_pad") or {}
        for key in sorted(by_pad):
            rec = by_pad[key]
            if p.id in (rec.get("pieces") or ()):
                return sorted(rec.get("orig") or ()), [i for i in rec["pieces"] if i in pmap]
    if p.conn >= 0:
        c = memory(ctx).conn(ctx, p.conn)
        if c is not None:
            return [(p.conn, _end_of_piece(c, p))], [p.id]
    return [], [p.id]


def _fan_sync(ctx: Ctx) -> None:
    """After a rollback that crossed a re_escape: let fanout re-align its book and the terminals with the state."""
    sync = getattr(_dep("fanout"), "sync", None)
    if callable(sync):
        sync(ctx)


def _dogbone_pad(ctx: Ctx, group: list):
    """The pad a dog-bone serves: the pad of its net an end of its stub sits on (else the nearest within 3 mm)."""
    nid = ctx.oracle.net_id(group[0].net)
    pts = [(g.x1, g.y1) for g in group] + [(g.x2, g.y2) for g in group]
    best = None
    for pad in getattr(ctx.board, "pads", ()):
        if pad.net != nid:
            continue
        d = min(math.hypot(pad.x - x, pad.y - y) for x, y in pts)
        key = (round(d, 6), pad.name)
        if best is None or key < best[0]:
            best = (key, pad)
    return best[1] if best is not None and best[0][0] <= 3.0 else None


def _escape_side(ctx: Ctx, conn: ConnV2, end: str) -> str:
    """Compass side ('n'|'e'|'s'|'w', KiCad: +y is south) the escape of this end leaves its pad on."""
    t = conn.a if end == "a" else conn.b
    pad = memory(ctx).pad(ctx, t.pad)
    dx = dy = 0.0
    if pad is not None:
        dx, dy = t.x - pad.x, t.y - pad.y
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        o = conn.b if end == "a" else conn.a
        dx, dy = o.x - t.x, o.y - t.y
    if abs(dx) >= abs(dy):
        return "e" if dx >= 0 else "w"
    return "s" if dy > 0 else "n"


def _end_of_piece(conn: ConnV2, p: Piece) -> str:
    mx, my = (p.x1 + p.x2) / 2, (p.y1 + p.y2) / 2
    da = math.hypot(conn.a.x - mx, conn.a.y - my)
    db = math.hypot(conn.b.x - mx, conn.b.y - my)
    return "a" if da <= db else "b"


def _rip_and_route(ctx: Ctx, conn: ConnV2, victims: list[int], pids, budget: Budget) -> tuple[bool, list]:
    """One transaction: rip the victims, move crossed escapes, route ``conn``, re-place crossed dog-bones, re-route
    the victims. -> (accepted, blamed victims). Anything short of "score improved and every victim routed again"
    is rolled back."""
    mem = memory(ctx)
    state = ctx.state
    before = state.score()
    victims = list(victims)
    saved = _save(ctx, [conn.idx] + victims)
    pmap = _piece_map(ctx)
    crossed_pieces = [pmap[i] for i in sorted(pids or ()) if i in pmap]
    blame: list = list(victims)
    mem.stats["rips"] += 1
    pbook = (ctx.history.get("power") or {}).get("dogbones") if isinstance(ctx.history.get("power"), dict) else None
    pbook0 = dict(pbook) if isinstance(pbook, dict) else None
    moved = [False]
    tok = state.begin()

    def fail(who=None, why=""):
        state.rollback(tok)
        if moved[0]:
            _fan_sync(ctx)
        _restore(ctx, saved)
        if pbook0 is not None:
            pbook.clear()
            pbook.update(pbook0)
        mem.stats["rollbacks"] += 1
        mem.event("mincut", conn.idx, victims=list(victims), ok=False, why=why,
                  blamed=list(blame if who is None else who))
        return False, (blame if who is None else who)

    # crossed reservations. A dog-bone is only ripped together with a replacement for the same pad; an escape stub
    # only when every connection of its pad can be ripped and the pad escapes another way.
    bones, moves = [], []
    seen: set = set()
    for p in crossed_pieces:
        if not p.protected or p.id in seen:
            continue
        if p.stage == "power":
            pad, group = _dogbone_of(ctx, pmap, p)
            seen.update(g.id for g in group)
            if pad is None or _dep("power") is None:
                return fail([-1], "dog-bone without a pad")
            bones.append((pad, frozenset((q(g.x1), q(g.y1)) for g in group if g.kind == "via"), group))
        else:
            users, ids = _escape_users(ctx, pmap, p)
            seen.update(ids)
            if not users or _dep("fanout") is None:
                return fail([-1], "escape stub nobody can move")
            for ci, _end in users:
                uc = mem.conn(ctx, ci)
                if uc is None or _net_protected(ctx, uc.net):
                    return fail([-1], "escape stub of a protected net")
                _save(ctx, [ci], saved)
                if state.status(ci) == "routed" and ci not in victims:
                    victims.append(ci)
            moves.append((users[0], ids))
    for _pad, _sites, group in bones:
        for g in group:
            state.remove(g.id)
    for v in victims:
        _rip(ctx, v)
        mem.ripped[v] = mem.ripped.get(v, 0) + 1
    for (ci, end), ids in moves:
        uc = mem.conn(ctx, ci)
        moved[0] = True
        if not _dep("fanout").re_escape(ctx, uc, end, _escape_side(ctx, uc, end)):
            return fail([-1], "no other escape")
        if any(ctx.oracle.has_piece(i) for i in ids):
            return fail([-1], "escape not moved")
    extra = [] if _lazy(ctx) else _cascade(ctx, victims, saved)
    if budget.spent_out():
        return fail([], "budget")
    r = _route(ctx, conn, budget, record=False, widen=2)
    if not r["ok"]:
        return fail(None, "no route even with the victims ripped: " + _why(r))
    power = _dep("power")
    for pad, sites, _group in bones:
        if not power.place_dogbone(ctx, pad, exclude=sites):
            return fail([-1], "no other dog-bone site")
    for v in sorted(victims + extra, key=lambda i: _rank(mem.conn(ctx, i))):
        if budget.spent_out():
            return fail([], "budget")
        vc = mem.conn(ctx, v)
        rv = _reroute(ctx, vc, budget)
        if not rv["ok"] and not budget.spent_out():
            rv = _last_resort(ctx, vc, budget, VICTIM_FINE) or rv
        if not rv["ok"]:
            # what does the victim fight now? those connections share the channel (permutation candidates)
            if not budget.spent_out():
                soft = _soft_table(ctx, vc, False, frozenset((conn.idx,)))
                if soft:
                    t2 = state.begin()
                    r2 = _route(ctx, vc, budget, record=False, widen=2, soft_cost=_pricer(soft))
                    state.rollback(t2)
                    mem.stats["soft_searches"] += 1
                    mem.push(mem.contested, conn.idx, [v] + sorted(k for k in r2["crossed"] if k >= 0 and k != conn.idx))
            return fail([v], f"victim {v} cannot be re-routed: " + _why(rv))
    if _lazy(ctx):
        # connections of a victim's net that are apart now that the victim runs elsewhere (they teed onto its old
        # copper, or their terminals were joined through it): those are routed again, and only those
        extra = _cascade(ctx, victims, saved)
        for v in sorted(extra, key=lambda i: _rank(mem.conn(ctx, i))):
            if budget.spent_out():
                return fail([], "budget")
            rv = _reroute(ctx, mem.conn(ctx, v), budget)
            if not rv["ok"]:
                return fail([v], f"connection {v} that hung on a victim cannot be re-routed: " + _why(rv))
    after = state.score()
    if not (after < before) or _unrouted(ctx, victims + extra):
        return fail(None, "score not improved")
    state.commit(tok)
    mem.stats["accepted"] += 1
    mem.event("mincut", conn.idx, victims=victims + extra, ok=True)
    return True, []


def _why(r: dict) -> str:
    f = r.get("fail") or {}
    return str(f.get("code") or f.get("reason") or "failed")[:80]


def _pricer(table: dict):
    """The soft_cost detail.route_conn takes: callable(Piece) -> mm, None = this piece stays hard."""
    return lambda piece: table.get(piece.id)


def mincut_rip(ctx: Ctx, conn: ConnV2, budget: Budget, tabu: set) -> bool:
    """Design §4.9 step 1. True when ``conn`` is routed afterwards (and every connection routed before still is)."""
    mem = memory(ctx)
    state = ctx.state
    if _dep("detail") is None:
        return False
    mem.note(conn.idx, "mincut")
    protected_ok = mem.piece_info is not False
    hard: set = set()
    attempts = 0
    while attempts < MAX_RIP_ATTEMPTS and not budget.spent_out():
        soft = _soft_table(ctx, conn, protected_ok, frozenset(hard))
        if not soft:
            if not hard:
                mem.static_cut.add(conn.idx)             # nothing rippable on the board
            return False
        saved = _save(ctx, [conn.idx])
        tok = state.begin()
        r = _route(ctx, conn, budget, record=False, widen=2, soft_cost=_pricer(soft))
        mem.stats["soft_searches"] += 1
        crossed = r["crossed"]
        if r["ok"] and not crossed:                      # the board changed since it failed: plain success
            state.commit(tok)
            return True
        state.rollback(tok)
        _restore(ctx, saved)
        if not crossed:
            if not hard and not budget.spent_out():
                mem.static_cut.add(conn.idx)             # no path even through every rippable piece
            return False
        cp = r.get("crossed_pieces")
        pids = None
        if cp is None:
            mem.piece_info = False
            if protected_ok:                             # cannot tell which protected piece was crossed: make them hard
                protected_ok = False
                if any(k < 0 for k in crossed) or any(p.protected and p.conn in crossed for p in state.pieces()):
                    continue
        else:
            mem.piece_info = True
            pids = sorted(int(k) for k in cp)
        attempts += 1
        victims = sorted((k for k in crossed if k >= 0 and mem.conn(ctx, k) is not None),
                         key=lambda i: _rank(mem.conn(ctx, i)))
        if any(k < 0 for k in crossed) and pids is None:
            return False
        if pids is not None:
            pmap = _piece_map(ctx)
            mem.spots[conn.idx] = [((pmap[i].x1 + pmap[i].x2) / 2, (pmap[i].y1 + pmap[i].y2) / 2, pmap[i].layer)
                                   for i in pids if i in pmap]
        mem.push(mem.mincut, conn.idx, victims)
        key = (conn.idx, frozenset(victims) | (frozenset((-1,)) if any(k < 0 for k in crossed) else frozenset()))
        if key in tabu:
            mem.stats["tabu_hits"] += 1
            blamed = mem.blame.get(key) or victims
        else:
            tabu.add(key)
            ok, blamed = _rip_and_route(ctx, conn, victims, pids, budget)
            if ok:
                return True
            mem.blame[key] = list(blamed)
            _bump(ctx, conn, victims, pids)
        # look for a different cut: the connections that could not move are walls for the rest of this ladder
        new = [v for v in (blamed or victims) if v >= 0 and v not in hard]
        if not new:
            new = [v for v in victims if v not in hard]
        if not new:
            return False
        hard.update(new)
    return False


# ── step 2: cluster permutation ─────────────────────────────────────────────────────────────────────
def _where(ctx: Ctx, conn: ConnV2) -> tuple[float, float]:
    f = _fail_of(ctx, conn.idx) or {}
    w = f.get("where")
    if isinstance(w, (list, tuple)) and len(w) >= 2 and all(isinstance(v, (int, float)) for v in w[:2]):
        return float(w[0]), float(w[1])
    return (conn.a.x + conn.b.x) / 2, (conn.a.y + conn.b.y) / 2


def _cluster(ctx: Ctx, conn: ConnV2) -> list[ConnV2]:
    """The connections that fight for the channel of ``conn``: failed ones, its min-cut victims and what they
    fight, and connections ripped twice, inside a 12 x 12 mm box around the failure; at most 5, nearest first."""
    mem = memory(ctx)
    cx, cy = _where(ctx, conn)
    half = CLUSTER_BOX_MM / 2
    named = set(mem.mincut.get(conn.idx, ())) | set(mem.contested.get(conn.idx, ()))
    cands = []
    for c in ctx.conns:
        if c.idx == conn.idx or _net_protected(ctx, c.net):
            continue
        st = ctx.state.status(c.idx)
        if st != "routed":
            prio = 0
            pts = [(c.a.x, c.a.y), (c.b.x, c.b.y)]
            if st == "failed":
                pts.append(_where(ctx, c))
        elif c.idx in named:
            prio = 1
            pts = None
        elif mem.ripped.get(c.idx, 0) >= 2:
            prio = 2
            pts = None
        else:
            continue
        if pts is None:                                  # routed: the point of each piece nearest the failure
            pts = []
            for p in ctx.state.pieces(conn=c.idx):
                dx, dy = p.x2 - p.x1, p.y2 - p.y1
                L = dx * dx + dy * dy
                t = 0.0 if L <= 0 else max(0.0, min(1.0, ((cx - p.x1) * dx + (cy - p.y1) * dy) / L))
                pts.append((p.x1 + t * dx, p.y1 + t * dy))
        inside = [math.hypot(x - cx, y - cy) for x, y in pts if abs(x - cx) <= half and abs(y - cy) <= half]
        if inside:
            cands.append((prio, round(min(inside), 6), _rank(c), c))
    cands.sort(key=lambda t: t[:3])
    return sorted([conn] + [t[3] for t in cands[:CLUSTER_MAX - 1]], key=_rank)


def _route_perm(ctx: Ctx, conn: ConnV2, budget: Budget) -> dict:
    """A member of a permuted cluster: the plain route, then the last resort (bridge, fine lattice)."""
    r = _route(ctx, conn, budget, record=False)
    if r["ok"] or budget.spent_out():
        return r
    r2 = _last_resort(ctx, conn, budget, PERM_FINE)
    return r2 if r2 is not None and r2["ok"] else r


def permute_cluster(ctx: Ctx, conns: list[ConnV2], budget: Budget) -> bool:
    """Design §4.9 step 2: rip the whole cluster and route it in every order (lexicographic over ``rank``, at
    most 120 orders, a shared prefix is routed once). The first order that routes all of them is kept; else the
    order with the fewest failures, if it beats the present state. True when the state was improved.
    ``budget`` is the per-cluster cap."""
    mem = memory(ctx)
    state = ctx.state
    conns = sorted(conns, key=_rank)[:CLUSTER_MAX]
    if len(conns) < 2 or _dep("detail") is None:
        return False
    for c in conns:
        mem.note(c.idx, "permute")
    idxs = [c.idx for c in conns]
    before = state.score()
    saved = _save(ctx, idxs)
    base_fails = _unrouted(ctx, idxs)
    outer = state.begin()
    for c in conns:
        _rip(ctx, c.idx)
    extra = [] if _lazy(ctx) else _cascade(ctx, idxs, saved)
    core = list(idxs)
    late_all: list = []
    tail: list = []
    if extra:
        # tee-dependents come along: as members of the cluster while there is room; else as its tail — routed in
        # rank order after every order of the cluster (they hang on a member's copper, their order among the
        # members is not what the channel is contested for)
        if len(conns) + len(extra) > CLUSTER_MAX:
            if len(extra) > TAIL_MAX:
                state.rollback(outer)
                _restore(ctx, saved)
                mem.event("permute", conns[0].idx, cluster=list(idxs), orders=0, ok=False,
                          why=f"{len(extra)} connections hang on the cluster's copper")
                return False
            tail = sorted((mem.conn(ctx, i) for i in extra), key=_rank)
            idxs = idxs + [c.idx for c in tail]
        else:
            conns = sorted(conns + [mem.conn(ctx, i) for i in extra], key=_rank)
            idxs = [c.idx for c in conns]
    best = {"fails": base_fails, "order": None}
    box = {"orders": 0, "found": False}
    # a failed member that cannot be routed with the whole cluster out of the way fails in every order
    stuck = 0
    for c in conns:
        if saved[c.idx][0] == "routed" or budget.spent_out():
            continue
        tok = state.begin()
        r = _route_perm(ctx, c, budget)
        state.rollback(tok)
        if state.status(c.idx) != "todo":
            state.set_status(c.idx, "todo")
        if not r["ok"]:
            stuck += 1
    if stuck >= base_fails:
        state.rollback(outer)
        _restore(ctx, saved)
        mem.stats["rollbacks"] += 1
        mem.event("permute", conns[0].idx, cluster=list(idxs), orders=0, ok=False,
                  why="a failed member cannot be routed even with the cluster ripped")
        return False

    def dfs(remaining: list, order: list, fails: int) -> None:
        if not remaining:
            box["orders"] += 1
            mem.stats["perm_orders"] += 1
            t2 = state.begin() if (tail or _lazy(ctx)) else None
            late = _cascade(ctx, core, saved) if _lazy(ctx) else []
            for c in sorted((mem.conn(ctx, i) for i in late), key=_rank) + tail:
                if fails >= best["fails"]:
                    break
                if not _route_perm(ctx, c, budget)["ok"]:
                    fails += 1
            if fails < best["fails"]:
                best["fails"], best["order"] = fails, list(order)
            if fails == 0:
                box["found"] = True
                late_all.extend(late)
                if t2 is not None:
                    state.commit(t2)
                return
            if t2 is not None:
                state.rollback(t2)
                for c in tail:
                    if state.status(c.idx) != "todo":
                        state.set_status(c.idx, "todo")
                for i in late:
                    if state.status(i) != "routed":
                        state.set_status(i, "routed")
            return
        for c in remaining:
            if box["found"] or box["orders"] >= PERM_MAX or budget.spent_out():
                return
            tok = state.begin()
            r = _route_perm(ctx, c, budget)
            f2 = fails + (0 if r["ok"] else 1)
            if f2 < best["fails"]:
                dfs([x for x in remaining if x is not c], order + [c], f2)
            else:
                box["orders"] += 1                       # this prefix cannot beat the best: its orders are skipped
            if box["found"]:
                state.commit(tok)
                return
            state.rollback(tok)
            if state.status(c.idx) != "todo":
                state.set_status(c.idx, "todo")

    dfs(conns, [], 0)
    if not box["found"] and best["order"] is not None:   # replay the best partial order (deterministic)
        for c in best["order"]:
            r = _route_perm(ctx, c, budget)
            if not r["ok"]:
                _set_failed(ctx, c.idx, r.get("fail") or saved[c.idx][1])
        late = _cascade(ctx, core, saved) if _lazy(ctx) else []
        late_all.extend(late)
        for c in sorted((mem.conn(ctx, i) for i in late), key=_rank) + tail:
            r = _route_perm(ctx, c, budget)
            if not r["ok"]:
                _set_failed(ctx, c.idx, r.get("fail") or saved[c.idx][1])
    improved = (box["found"] or best["order"] is not None) and state.score() < before \
        and _unrouted(ctx, idxs + late_all) < base_fails
    mem.event("permute", conns[0].idx, cluster=list(idxs), orders=box["orders"], ok=bool(improved),
              fails=best["fails"], was=base_fails)
    if improved:
        state.commit(outer)
        mem.stats["perm_solved"] += 1
        return True
    state.rollback(outer)
    _restore(ctx, saved)
    mem.stats["rollbacks"] += 1
    nets = sorted({c.net for c in conns})
    for c in conns:
        if state.status(c.idx) != "routed":
            mem.channel[c.idx] = {"conns": list(idxs), "nets": nets, "orders": box["orders"]}
    return False


# ── step 3: rescue ──────────────────────────────────────────────────────────────────────────────────
def rescue(ctx: Ctx, conn: ConnV2, budget: Budget) -> bool:
    """Design §4.9 step 3, each rung in its own transaction: one extra via (above ``max_vias`` and outside the
    layer assignment of the global stage); half window pitch; neck width (with both of the above); the other
    fan-out side (only this connection's own escape is moved)."""
    mem = memory(ctx)
    state = ctx.state
    if _dep("detail") is None or _hopeless(ctx, conn):
        return False
    mem.note(conn.idx, "rescue")
    saved = _save(ctx, [conn.idx])

    def attempt(rung, pre=None, neck=False, free=False, **kw) -> bool:
        if budget.spent_out():
            return False
        tok = state.begin()
        if free:
            _free_layers(ctx, conn)
        moved = pre is not None
        if moved and not pre():
            state.rollback(tok)
            _fan_sync(ctx)
            _restore(ctx, saved)
            mem.event("rescue", conn.idx, rung=rung, ok=False, why="no other escape")
            return False
        have = {p.id for p in state.pieces(conn=conn.idx)}
        full = conn.width
        if neck:
            conn.width = conn.neck
        try:
            r = _route(ctx, conn, budget, record=False, **kw)
        finally:
            conn.width = full
        if not r["ok"]:
            state.rollback(tok)
            if moved:
                _fan_sync(ctx)
            _restore(ctx, saved)
            mem.event("rescue", conn.idx, rung=rung, ok=False, why=_why(r))
            return False
        if neck:                                         # routed at neck width: polish should restore the full width
            for p in sorted(state.pieces(conn=conn.idx), key=lambda p: p.id):
                if p.id in have or p.kind != "seg" or p.target_w >= full:
                    continue
                d = dataclasses.asdict(p)
                d.pop("id")
                d["target_w"] = full
                state.remove(p.id)
                state.add(**d)
        state.set_status(conn.idx, "routed")
        state.commit(tok)
        mem.stats["rescued"] += 1
        mem.event("rescue", conn.idx, rung=rung, ok=True)
        return True

    # cheapest first: a via the global layer assignment did not plan costs a normal search, half the pitch four
    if attempt("extra via", free=True, widen=2, extra_vias=1):
        return True
    if attempt("half pitch", widen=2, pitch_scale=0.5):
        return True
    if 0 < conn.neck < conn.width - 1e-9 and attempt("neck", neck=True, free=True, widen=2, pitch_scale=0.5,
                                                     extra_vias=1):
        return True
    fan = _dep("fanout")
    if fan is not None:
        for end in ("a", "b"):
            if not (conn.a if end == "a" else conn.b).escape:
                continue
            side = _escape_side(ctx, conn, end)
            if attempt("escape " + end, pre=lambda end=end, side=side: bool(fan.re_escape(ctx, conn, end, side)),
                       widen=2):
                return True
    return False


# ── step 4: global feedback ─────────────────────────────────────────────────────────────────────────
def _borders(ctx: Ctx, conn: ConnV2) -> list[tuple]:
    """Tile borders named by a failure: the borders of the tile of its ``where`` that its corridor crosses."""
    tiles = ctx.tiles
    cor = ctx.corridors.get(conn.idx)
    if tiles is None or cor is None:
        return []
    x, y = _where(ctx, conn)
    tx, ty = tiles.tile_of(x, y)
    layers = tuple(cor.layers) or tuple(range(tiles.nl))
    out = []
    for ex, ey, side, nb in ((tx, ty, "E", (tx + 1, ty)), (tx - 1, ty, "E", (tx - 1, ty)),
                             (tx, ty, "N", (tx, ty + 1)), (tx, ty - 1, "N", (tx, ty - 1))):
        if nb not in cor.tiles or not (0 <= ex < tiles.nx and 0 <= ey < tiles.ny):
            continue
        a = (ex, ey)
        b = (ex + 1, ey) if side == "E" else (ex, ey + 1)
        for layer in layers:
            out.append((layer, ex, ey, side, a, b))
    return out


def _feedback(ctx: Ctx, failed: list[ConnV2], budget: Budget) -> int:
    """One feedback loop. -> connections gained (0 = rolled back)."""
    mem = memory(ctx)
    state = ctx.state
    tiles = ctx.tiles
    g = _dep("globalroute")
    if tiles is None or g is None or _dep("detail") is None:
        return 0
    booked = []
    pairs = []
    for c in failed:
        demand = (c.width or mem.wmin(ctx)) + _clearance(ctx, c)
        for layer, ex, ey, side, a, b in _borders(ctx, c):
            booked.append((layer, ex, ey, side, demand))
            if (a, b) not in pairs:
                pairs.append((a, b))
    if not booked:
        return 0
    mem.feedback_loops += 1
    for c in failed:
        mem.note(c.idx, "feedback")
    for layer, ex, ey, side, mm in booked:
        tiles.book(layer, ex, ey, side, mm)
    affected = list(failed)
    have = {c.idx for c in failed}
    for c in sorted(ctx.conns, key=_rank):
        if len(affected) >= FEEDBACK_MAX_CONNS:
            break
        if c.idx in have or state.status(c.idx) != "routed" or _net_protected(ctx, c.net):
            continue
        cor = ctx.corridors.get(c.idx)
        if cor is not None and any(a in cor.tiles and b in cor.tiles for a, b in pairs):
            affected.append(c)
            have.add(c.idx)
    idxs = [c.idx for c in affected]
    before = state.score()
    base_fails = _unrouted(ctx, idxs)
    saved = _save(ctx, idxs)
    old_cor = {i: ctx.corridors.get(i) for i in idxs}
    tok = state.begin()
    for c in affected:
        if state.status(c.idx) == "routed":
            _rip(ctx, c.idx)
    extra = _cascade(ctx, idxs, saved)
    order = sorted(affected + [mem.conn(ctx, i) for i in extra], key=_rank)
    g.run(ctx, budget.sub(0.3), only=sorted(idxs))
    for c in order:
        if budget.spent_out():
            break
        r = _route(ctx, c, budget, record=False)
        if not r["ok"]:
            _set_failed(ctx, c.idx, r.get("fail") or saved[c.idx][1])
    all_idx = idxs + extra
    now = _unrouted(ctx, all_idx)
    if state.score() < before and now < base_fails:
        state.commit(tok)
        mem.stats["feedback_gain"] += base_fails - now
        return base_fails - now
    state.rollback(tok)
    _restore(ctx, saved)
    mem.stats["rollbacks"] += 1
    for i, cor in old_cor.items():
        if cor is None:
            ctx.corridors.pop(i, None)
        else:
            ctx.corridors[i] = cor
    for layer, ex, ey, side, mm in booked:               # the booking described a routing that was not kept
        try:
            tiles.book(layer, ex, ey, side, -mm)
        except Exception:  # noqa: BLE001
            pass
    return 0


# ── step 5: certificate ─────────────────────────────────────────────────────────────────────────────
def _first_wall(ctx: Ctx, net: str, layer: int, x: float, y: float, dx: float, dy: float, reach: float):
    """March a hair-thin probe of ``net`` from (x, y) along (dx, dy). -> (static Blocker | None, distance to its
    face along the ray, [piece ids of routed copper passed on the way])."""
    o = ctx.oracle
    step = 0.5
    hair = 0.01
    passed: list = []
    t = 0.0
    while t < reach - 1e-9:
        t2 = min(reach, t + step)
        bs = o.blockers(layer, x + dx * t, y + dy * t, x + dx * t2, y + dy * t2, hair, net, limit=24)
        wall = None
        for b in bs:
            if b.rule not in HARD_RULES:
                continue
            if b.static:
                if wall is None:
                    wall = b
            elif b.piece is not None and b.piece not in passed:
                passed.append(b.piece)
        if wall is not None:
            # refine: the first point of the step that sees the wall is one clearance (wall.need) away from its face
            lo, hi = t, t2

            def sees(tt, tag=wall.tag):
                return any(b.static and b.tag == tag and b.rule in HARD_RULES
                           for b in o.blockers(layer, x + dx * tt, y + dy * tt, x + dx * tt, y + dy * tt, hair, net,
                                               limit=24))
            if sees(lo):
                d = 0.0
                for b in o.blockers(layer, x + dx * lo, y + dy * lo, x + dx * lo, y + dy * lo, hair, net, limit=24):
                    if b.static and b.tag == wall.tag:
                        d = max(0.0, b.dist)
                        break
                return wall, lo + d, passed
            for _ in range(7):
                mid = (lo + hi) / 2
                if sees(mid):
                    hi = mid
                else:
                    lo = mid
            return wall, hi + max(0.0, wall.need), passed
        t = t2
    return None, reach, passed


def _transversal(ctx: Ctx, conn: ConnV2, x: float, y: float, layer: int, dx: float, dy: float):
    """The cut through (x, y) along +-(dx, dy): fixed copper on both sides, every routed net between them.
    -> Cut | None (None when one side is open within CUT_REACH_MM)."""
    mem = memory(ctx)
    wa, da, pa = _first_wall(ctx, conn.net, layer, x, y, dx, dy, CUT_REACH_MM)
    if wa is None:
        return None
    wb, db, pb = _first_wall(ctx, conn.net, layer, x, y, -dx, -dy, CUT_REACH_MM)
    if wb is None:
        return None
    cl = _clearance(ctx, conn)
    pmap = _piece_map(ctx)
    demand = (conn.neck if 0 < conn.neck < conn.width else conn.width) + cl
    nets = [conn.net]
    seen: set = set()
    for pid in pa + pb:
        p = pmap.get(pid)
        if p is None or p.kind != "seg":
            continue
        key = p.conn if p.conn >= 0 else ("res", p.net, p.id)
        if key in seen:
            continue
        seen.add(key)
        c = mem.conn(ctx, p.conn) if p.conn >= 0 else None
        demand += p.w + (_clearance(ctx, c) if c is not None else cl)
        if p.net not in nets:
            nets.append(p.net)
    gap = da + db
    a_tag, b_tag = wa.tag or "?", wb.tag or "?"
    if a_tag != "edge" and b_tag != "edge" and a_tag != b_tag:
        try:
            g = ctx.oracle.pad_gap(a_tag, b_tag, layer)
            if g != math.inf and g > 0:
                gap = g
        except Exception:  # noqa: BLE001
            pass
    names = sorted((a_tag, b_tag))
    return Cut(a=names[0], b=names[1], layer=_layer_name(ctx, layer), capacity_mm=round(max(0.0, gap - cl), 3),
               demand_mm=round(demand, 3), nets=nets)


def _layer_name(ctx: Ctx, layer: int) -> str:
    layers = getattr(ctx.board, "layers", None) or []
    return layers[layer] if 0 <= layer < len(layers) else str(layer)


def _exit_cut(ctx: Ctx, conn: ConnV2, end: str):
    """A boxed-in terminal: the widest gap between two neighbouring pieces of fixed copper around it (the exit
    that needs the smallest change). -> Cut | None"""
    t = conn.a if end == "a" else conn.b
    w = conn.neck if 0 < conn.neck < conn.width else conn.width
    cl = _clearance(ctx, conn)
    reach = _pad_radius(ctx, t) + 3.0 * (w + cl)
    best = None
    for layer in t.layers:
        ring = []
        for dx, dy in _DIRS16:
            wall, d, _p = _first_wall(ctx, conn.net, layer, t.x, t.y, dx, dy, reach)
            ring.append(None if wall is None else (wall.tag or "?", (t.x + dx * d, t.y + dy * d)))
        for i in range(16):
            a, b = ring[i], ring[(i + 1) % 16]
            if a is None or b is None or a[0] == b[0]:
                continue
            gap = math.hypot(a[1][0] - b[1][0], a[1][1] - b[1][1])
            cut = None
            if a[0] != "edge" and b[0] != "edge":
                tiles = ctx.tiles
                if tiles is not None:
                    try:
                        cut = tiles.pad_cut(a[0], b[0], layer, w, cl)
                    except Exception:  # noqa: BLE001
                        cut = None
                if cut is None:
                    try:
                        g = ctx.oracle.pad_gap(a[0], b[0], layer)
                        if g != math.inf:
                            gap = g
                    except Exception:  # noqa: BLE001
                        pass
            if cut is None:
                names = sorted((a[0], b[0]))
                cut = Cut(a=names[0], b=names[1], layer=_layer_name(ctx, layer),
                          capacity_mm=round(max(0.0, gap - cl), 3), demand_mm=round(w + cl, 3), nets=[conn.net])
            else:
                if not cut.nets:
                    cut.nets = [conn.net]
                if not cut.demand_mm:
                    cut.demand_mm = round(w + cl, 3)
            key = (-cut.capacity_mm, cut.a, cut.b, cut.layer)
            if best is None or key < best[0]:
                best = (key, cut)
    return best[1] if best else None


def _channel_cut(ctx: Ctx, conn: ConnV2):
    """The tightest transversal cut around the places where ``conn`` is contested. -> Cut | None"""
    mem = memory(ctx)
    pts: list = []
    for x, y, layer in mem.spots.get(conn.idx, ()):
        if layer >= 0:
            pts.append((q(x), q(y), layer))
    wx, wy = _where(ctx, conn)
    layers = sorted(set(conn.a.layers) | set(conn.b.layers))[:2]
    mx, my = (conn.a.x + conn.b.x) / 2, (conn.a.y + conn.b.y) / 2
    for v in mem.mincut.get(conn.idx, ()):               # the piece of each victim nearest the straight line
        ps = [p for p in ctx.state.pieces(conn=v) if p.kind == "seg"]
        if ps:
            p = min(ps, key=lambda p: (round(math.hypot((p.x1 + p.x2) / 2 - mx, (p.y1 + p.y2) / 2 - my), 6), p.id))
            pts.append((q((p.x1 + p.x2) / 2), q((p.y1 + p.y2) / 2), p.layer))
    for layer in layers:
        pts.append((q(wx), q(wy), layer))
    uniq = []
    for p in pts:
        if p not in uniq and ctx.oracle.inside(p[0], p[1]):
            uniq.append(p)
    best = None
    s = math.sqrt(0.5)
    for x, y, layer in uniq[:6]:
        for dx, dy in ((1.0, 0.0), (0.0, 1.0), (s, s), (s, -s)):
            cut = _transversal(ctx, conn, x, y, layer, dx, dy)
            if cut is None:
                continue
            key = (round(cut.capacity_mm - cut.demand_mm, 3), cut.capacity_mm, cut.a, cut.b, cut.layer)
            if best is None or key < best[0]:
                best = (key, cut)
    return best[1] if best else None


def _edge_cut(ctx: Ctx, conn: ConnV2):
    """Fallback when no geometric cut was found: the tile border the corridor crosses at the failure."""
    tiles = ctx.tiles
    bs = _borders(ctx, conn)
    if tiles is None or not bs:
        return None
    layer, ex, ey, side = bs[0][:4]
    try:
        return tiles.cut_for_edge(layer, ex, ey, side, [(conn.net, conn.width + _clearance(ctx, conn))])
    except Exception:  # noqa: BLE001
        return None


def _suggest(ctx: Ctx, conn: ConnV2, cert: dict, cut) -> list[dict]:
    """Ready plan fragments (existing plan fields only) or a placement move, most specific first."""
    mem = memory(ctx)
    out = []
    cl = _clearance(ctx, conn)
    w = conn.neck if 0 < conn.neck < conn.width else conn.width
    if cut is not None and cut.demand_mm > cut.capacity_mm:
        short = round(cut.demand_mm - cut.capacity_mm, 3)
        pa, pb = mem.pad(ctx, cut.a), mem.pad(ctx, cut.b)
        if pa is not None and pb is not None and pa.ref != pb.ref:
            dx, dy = pb.x - pa.x, pb.y - pa.y
            n = math.hypot(dx, dy) or 1.0
            by = math.ceil((short + 0.05) * 20) / 20
            out.append({"why": f"the gap between {cut.a} and {cut.b} on {cut.layer} carries {cut.capacity_mm} mm of "
                               f"track + clearance but {cut.demand_mm} mm is wanted ({', '.join(cut.nets[:4])}): "
                               f"move {pb.ref} {by} mm away from {pa.ref}",
                        "patch": {"move": {"ref": pb.ref, "by": [round(dx / n * by, 3), round(dy / n * by, 3)]}}})
        elif cut.a == "edge" or cut.b == "edge":
            part = cut.b if cut.a == "edge" else cut.a
            pp = mem.pad(ctx, part)
            if pp is not None:
                out.append({"why": f"only {cut.capacity_mm} mm between {part} and the board edge on {cut.layer} for "
                                   f"{cut.demand_mm} mm of track + clearance: move {pp.ref} {short + 0.05:.2f} mm inwards "
                                   f"or route {conn.net} on another layer",
                            "patch": {"nets": {conn.net: {"priority": 90}}}})
    wmin = (getattr(ctx.board, "rules", None) or {}).get("track_width") or 0.0
    if cut is not None and 0 < wmin < w and cut.capacity_mm < cut.demand_mm <= cut.capacity_mm + (w - wmin) + 1e-9:
        out.append({"why": f"{conn.net} fits the cut at the board minimum width {wmin} mm",
                    "patch": {"nets": {conn.net: {"width": wmin}}}})
    nets = cert.get("mincut_nets") or []
    if nets:
        out.append({"why": f"{conn.net} is walled in by {', '.join(nets[:4])}, routed before it: route it first",
                    "patch": {"nets": {conn.net: {"priority": 90}}}})
    reason = cert.get("reason") or ""
    if "time budget" in reason or "effort" in reason:
        tb = getattr(ctx.cfg, "time_budget", None)
        if isinstance(tb, (int, float)) and tb < 600:
            out.append({"why": "the search budget ran out before the repair ladder finished",
                        "patch": {"global": {"time_budget_s": int(min(600, max(tb * 2, tb + 30)))}}})
    seen = []
    uniq = []
    for s in out:
        if s["patch"] not in seen:
            seen.append(s["patch"])
            uniq.append(s)
    return uniq[:4]


def certificate(ctx: Ctx, conn: ConnV2, budget: Budget | None = None) -> dict:
    """The ``failed[]`` record of a connection the ladder gave up on (design §4.9 step 5, §6.2). Keeps every key
    of the detail stage's fail record and adds / completes: reason, where, cut, blocked_by (ranked), blockers,
    static_share, mincut_nets, probe, tried, suggest."""
    mem = memory(ctx)
    c0 = ctx.oracle.checks
    old = dict(_fail_of(ctx, conn.idx) or {})
    pr = mem.probe.get(conn.idx)
    if pr is None:
        pr = mem.probe[conn.idx] = probe(ctx, conn, budget)
    tried = list(mem.tried.get(conn.idx, ()))
    victims = [v for v in mem.mincut.get(conn.idx, ()) if mem.conn(ctx, v) is not None]
    mincut_nets = []
    for v in victims:
        n = mem.conn(ctx, v).net
        if n not in mincut_nets:
            mincut_nets.append(n)
    old_probe = old.get("probe") if isinstance(old.get("probe"), dict) else {}
    enclosed = old_probe.get("enclosed") if old_probe.get("enclosed") in ("a", "b") else pr["enclosed"]

    cut = None
    if not _hopeless(ctx, conn):
        if enclosed in ("a", "b") and not mincut_nets:
            cut = _exit_cut(ctx, conn, enclosed)
        if cut is None:
            cut = _channel_cut(ctx, conn)
            if cut is not None and cut.demand_mm <= cut.capacity_mm:
                cut = None                               # the tightest transversal still has room: not the reason
        if cut is None and enclosed in ("a", "b"):
            cut = _exit_cut(ctx, conn, enclosed)
        if cut is None:
            cut = _edge_cut(ctx, conn)
    ch = mem.channel.get(conn.idx)
    if cut is not None and ch:
        for n in ch["nets"]:
            if n not in cut.nets:
                cut.nets.append(n)

    blocked = []
    for n in mincut_nets + [x for x in (old.get("blocked_by") or []) if isinstance(x, str)] \
            + [b["name"] for b in pr["blockers"]]:
        if n and n not in blocked and n != conn.net:
            blocked.append(n)
    if "static_share" in old and isinstance(old["static_share"], (int, float)):
        share = float(old["static_share"])
    elif pr["blockers"]:
        share = pr["static_share"]
    else:
        share = 1.0 if conn.idx in mem.static_cut or not mincut_nets else 0.0

    reason = old.get("reason") or ""
    decisive = any(k in reason for k in ("boxed in", "outside the board outline", "max_")) or _hopeless(ctx, conn)
    if not decisive:
        t = conn.a if enclosed == "a" else conn.b
        if _dep("detail") is None:
            reason = reason or "not repaired: the detail stage is unavailable"
        elif conn.idx in mem.starved or not (set(tried) - {"route", "probe"}):
            reason = "time budget used up before this connection could be repaired" + (f" ({reason})" if reason else "")
        elif ch:
            reason = "these nets cannot share this channel: " + ", ".join(ch["nets"][:6])
            if cut is not None:
                reason += (f" ({cut.capacity_mm} mm between {cut.a} and {cut.b} on {cut.layer} for "
                           f"{cut.demand_mm} mm of track + clearance)")
        elif enclosed in ("a", "b") and share >= 0.999:
            w = conn.neck if 0 < conn.neck < conn.width else conn.width
            reason = (f"pad {t.pad or 'terminal'} at ({round(t.x, 2)}, {round(t.y, 2)}) is boxed in: no exit for a "
                      f"{w} mm track between fixed copper")
        elif mincut_nets:
            reason = "blocked by routed nets: " + ", ".join(mincut_nets[:6])
        elif conn.idx in mem.static_cut:
            reason = "blocked by fixed copper: no path even with every other connection ripped up" \
                + (f" ({reason})" if reason else "")
        elif not reason:
            reason = "blocked"
    cert = dict(old)
    cert.update({
        "reason": reason,
        "where": old.get("where") or [round(v, 4) for v in _where(ctx, conn)],
        "cut": dataclasses.asdict(cut) if cut is not None else None,
        "blocked_by": blocked[:8],
        "blockers": pr["blockers"][:8],
        "static_share": round(share, 3),
        "mincut_nets": mincut_nets,
        "probe": {"enclosed": enclosed, "ends": pr["ends"]},
        "tried": [str(t) for t in (old.get("tried") or []) if str(t) not in LADDER] + tried,
        "ladder": tried,
    })
    cert["suggest"] = _suggest(ctx, conn, cert, cut)
    _charge_checks(ctx, budget, c0)
    return cert


# ── the stage ───────────────────────────────────────────────────────────────────────────────────────
def _hopeless(ctx: Ctx, conn: ConnV2) -> bool:
    """Failures no search can repair: a pad outside the outline, a pad on no layer its net may use."""
    f = _fail_of(ctx, conn.idx) or {}
    return f.get("code") in ("outside", "layer_pad") or "outside the board outline" in str(f.get("reason") or "")


def _static_only(ctx: Ctx, conn: ConnV2) -> bool:
    """The detail stage's own probe found a terminal walled in by fixed copper alone: nothing to rip."""
    f = _fail_of(ctx, conn.idx) or {}
    fp = f.get("probe") if isinstance(f.get("probe"), dict) else {}
    return fp.get("enclosed") in ("a", "b") and float(f.get("static_share") or 0.0) >= 0.999


def _rung_first(ctx: Ctx, conn: ConnV2, budget: Budget) -> bool:
    """A plain route for a connection nobody tried yet, then the probe."""
    mem = memory(ctx)
    state = ctx.state
    if state.status(conn.idx) == "todo":
        mem.note(conn.idx, "route")
        saved = _save(ctx, [conn.idx])
        tok = state.begin()
        r = _route(ctx, conn, budget)
        if r["ok"]:
            state.commit(tok)
            return True
        state.rollback(tok)
        _restore(ctx, saved)
        _set_failed(ctx, conn.idx, r.get("fail") or {"reason": ""})
    mem.note(conn.idx, "probe")
    mem.probe[conn.idx] = probe(ctx, conn, budget)
    return False


def _rung_mincut(ctx: Ctx, conn: ConnV2, budget: Budget) -> bool:
    if _static_only(ctx, conn) or _hopeless(ctx, conn):
        return False
    return mincut_rip(ctx, conn, budget, memory(ctx).tabu)


def _rung_permute(ctx: Ctx, conn: ConnV2, budget: Budget) -> bool:
    mem = memory(ctx)
    if _static_only(ctx, conn) or _hopeless(ctx, conn):
        return False
    cl = _cluster(ctx, conn)
    key = ("perm", frozenset(c.idx for c in cl))
    if len(cl) < 2 or key in mem.tabu:
        return False
    mem.tabu.add(key)
    permute_cluster(ctx, cl, budget)
    return ctx.state.status(conn.idx) == "routed"


def _passes() -> list:
    """(rung, share of the ladder budget). One rung is tried on every failed connection, in rank order, before the
    next rung starts: a connection that needs the expensive rungs cannot starve one a cheap rung would repair."""
    rip = [(_rung_mincut, 0.35), (_rung_permute, 0.25)]
    if RESCUE_FIRST:
        return [(_rung_first, 0.1), (rescue, 0.3)] + rip
    return [(_rung_first, 0.1)] + rip + [(rescue, 0.3)]


def run(ctx: Ctx, budget: Budget, only: list[int] | None = None) -> StageReport:
    """Repair every failed (or never tried) connection, ``only`` those listed when given. Connections it cannot
    repair end as 'failed' with a certificate (``state.set_status(conn, 'failed', certificate)``)."""
    mem = memory(ctx)
    state = ctx.state
    used0 = budget.used
    stats0 = dict(mem.stats)
    log0 = len(mem.log)
    rep = StageReport(name="repair")
    want = None if only is None else set(only)
    todo = [c for c in sorted(ctx.conns, key=_rank)
            if (want is None or c.idx in want) and state.status(c.idx) in ("failed", "todo")]
    if not todo:
        rep.score = state.score()
        return rep
    have_detail = _dep("detail") is not None
    if not have_detail:
        rep.notes.append("detail stage unavailable: nothing repaired, certificates only")
    fb = (ctx.tiles is not None and bool(ctx.corridors) and _dep("globalroute") is not None
          and have_detail and mem.feedback_loops < FEEDBACK_LOOPS)
    ladder = budget.sub(1.0 - FEEDBACK_SHARE) if fb else budget
    for c in todo:
        mem.starved.discard(c.idx)
    reached: set = set()
    passes = _passes() if have_detail else []
    for n, (rung, frac) in enumerate(passes):
        # a pass may use its own share plus what the passes before it left over; the last one everything
        if n == len(passes) - 1:
            pool = ladder
        else:
            due = frac + sum(f for _r, f in passes[:n])
            pool = ladder.sub(due - ladder.used / max(1, ladder.total))
        open_ = [c for c in todo if state.status(c.idx) != "routed"]
        for i, c in enumerate(open_):
            if state.status(c.idx) == "routed":          # repaired as part of a rip or a cluster
                continue
            share = pool.sub(pool.left() / max(1, pool.total) / (len(open_) - i)) if pool.total else pool
            if rung is _rung_first and state.status(c.idx) != "todo":
                rung(ctx, c, share)                      # only the probe: a few oracle checks, no search
                continue
            if pool.spent_out() or share.total < CHARGE_SEARCH:
                mem.starved.add(c.idx)
                continue
            reached.add(c.idx)
            if not rung(ctx, c, share) and share.spent_out() and state.status(c.idx) != "routed":
                mem.starved.add(c.idx)
    skipped = sum(1 for c in todo if c.idx not in reached and state.status(c.idx) != "routed") if have_detail else 0
    while fb and mem.feedback_loops < FEEDBACK_LOOPS and not budget.spent_out():
        failed = [c for c in todo if state.status(c.idx) != "routed" and not _hopeless(ctx, c)]
        if not failed:
            break
        loops = mem.feedback_loops
        gained = _feedback(ctx, failed, budget)
        if mem.feedback_loops == loops or not gained:    # nothing to book, or the loop was rolled back
            break
    certs = {}
    for c in todo:
        if state.status(c.idx) == "routed":
            continue
        cert = certificate(ctx, c, budget)
        _set_failed(ctx, c.idx, cert)
        certs[c.idx] = cert
    repaired = [c.idx for c in todo if state.status(c.idx) == "routed"]
    rep.routed = len(repaired)
    rep.failed = len(todo) - len(repaired)
    rep.score = state.score()
    rep.work = budget.used - used0
    delta = {k: mem.stats[k] - stats0.get(k, 0) for k in mem.stats}
    rep.data = {"repaired": repaired, "failed": sorted(certs), "certificates": certs, "stats": delta,
                "log": list(mem.log[log0:]), "tried": {c.idx: list(mem.tried.get(c.idx, ())) for c in todo},
                "feedback_loops": mem.feedback_loops, "tabu": len(mem.tabu)}
    if skipped:
        rep.notes.append(f"{skipped} connection(s) not attempted: repair budget used up")
    if delta["accepted"] or delta["rips"]:
        rep.notes.append(f"min-cut rip: {delta['accepted']} of {delta['rips']} accepted, {delta['tabu_hits']} tabu hit(s)")
    if delta["perm_orders"]:
        rep.notes.append(f"cluster permutation: {delta['perm_orders']} order(s) tried, {delta['perm_solved']} cluster(s) "
                         f"improved")
    if delta["rescued"]:
        rep.notes.append(f"rescue: {delta['rescued']} connection(s)")
    if delta["feedback_gain"]:
        rep.notes.append(f"global feedback: {delta['feedback_gain']} connection(s) gained")
    for i in sorted(certs):
        ch = mem.channel.get(i)
        if ch:
            rep.notes.append("these nets cannot share this channel: " + ", ".join(ch["nets"][:6]))
            break
    return rep
