"""S2 fanout: analytic fine-pitch escapes and via escapes (design §4.5).

What this stage does, per footprint that needs it (``needs_fanout``):

* **Rows** (QFP / QFN / SOIC / SMD connector sides) are found from the pad geometry (``footprint_rows``): at
  least three pads of one size on a line; the exit direction ``n`` is the outward row normal.
* **Ring fan** (``ring_fan``): every connected pad gets a straight stub from the pad centre outwards to a ring
  parallel to the row, optionally followed by a 45 degree leg to a slot on a higher ring. The geometry is
  generated analytically on the pad lattice and judged **only by the oracle**, so an at-the-rule 0.4 mm QFN
  escape is legal where a grid search finds nothing. Slots keep the pad order (planar by construction) and
  every connected pad of the row keeps a free outward lane, escaped or not (``_lane_ok``).
* **Via escape** (``via_escape``): pads whose partner is on another layer, pads the ring could not serve, and
  grid (BGA) pads get a stub to a through via. Pads are assigned to candidate via sites by min-cost flow
  (``flow.min_cost_flow``): nodes = pads, via columns (capacity 1: vias are through-only) and pad-pair cuts
  (capacity = tracks through the gap); cost = stub length + congestion. The flow proposes, the oracle
  disposes: a proposal that is illegal next to the escapes already placed is dropped and the flow re-solved;
  a stranded pad gets one augmenting swap. An outward via never closes the exit of a pad without a via.
* Each escape is committed to ``ctx.state`` as ``protected`` pieces with ``stage='fanout'`` and the terminal
  of every connection at that pad becomes an **escape terminal** (``Terminal.escape = True``) at the stub end.
* A pad with no legal escape whose exits are all closed by static copper is reported at once as ``pad boxed
  in`` with its pad-pair ``Cut`` and its connections are marked failed before any search budget is spent on
  them; a pad that merely got no escape is listed as unescaped and left to the detail stage.

Escape pieces carry ``conn = -1``: they belong to the pad, not to one connection (a pad can end several
connections, and ripping one of them must not tear the stub out from under the others). The pad -> escape
book-keeping lives in ``registry(ctx)``; ``sync(ctx)`` re-aligns terminals after a snapshot restore (``run``
hooks ``ctx.state.restore`` for that) or after somebody removed escape pieces; ``undo(ctx)`` removes every
escape. Decisions the design left open are listed in docs/internal/v2-interface-notes.md (W6).

Deterministic: parts by reference, rows by position, pads along the row, candidates in generation order; no
clock, no randomness; every coordinate goes through ``q()`` before the oracle sees it.

Measured (tests/test_v2_fanout.py): ~7 500 pads/s for the ring fan (3 oracle checks per pad), ~1 300 pads/s
for the via escape (50 checks per pad); the whole stage takes 0.01 - 0.3 s on the corpus boards.
"""
from __future__ import annotations

import math

from . import flow
from .types import (CHARGE_PATTERN, ORACLE_CHECKS_PER_UNIT, TOL_MM, Blocker, Budget, ConnV2, Ctx, Cut, Piece, StageReport,
                    Terminal, q)

LADDER = (0.25, 0.2, 0.15, 0.125, 0.1, 0.075, 0.05)     # window pitches (design §4.7)
RINGS = 3                   # rings parallel to a row
MIN_ROW = 3                 # pads on a line before it counts as a row
LINE_TOL = 0.01             # mm: pads this close to a common line are on it
VIA_DEPTHS = 3              # via rows in front of / behind a pad row
THERMAL_PRICE = 6.0         # x (w + cl) per thermal spoke a stub crosses (design §4.4: priced x6)
LEAN_TO_PARTNER = True      # pass 2b of the ring fan: lean a slot towards the partner where a lane is free
LANE_TOL = TOL_MM           # lanes are compared with the oracle's tolerance (pad positions are only exact to 1 nm)
SMD_KINDS = ("smd", "conn")
SIDE_VEC = {"n": (0.0, -1.0), "s": (0.0, 1.0), "e": (1.0, 0.0), "w": (-1.0, 0.0)}   # board y grows downwards
_S2 = math.sqrt(0.5)


# ── small helpers ─────────────────────────────────────────────────────────
def lattice_pitch(pad_pitch: float, w: float, cl: float) -> float:
    """The window pitch escapes are snapped to: the largest ladder value with p <= (w + cl) / 2 that divides
    the pad pitch (within 1 um); without one, the largest p <= min((w + cl) / 2, pitch / 4) (design §4.7 a, b)."""
    lim = (w + cl) / 2 + 1e-9
    for p in LADDER:
        if p <= lim and pad_pitch > 0 and abs(pad_pitch / p - round(pad_pitch / p)) * p <= 0.001:
            return p
    lim = min(lim, pad_pitch / 4 + 1e-9) if pad_pitch > 0 else lim
    for p in LADDER:
        if p <= lim:
            return p
    return LADDER[-1]


def _snap_up(v: float, p: float) -> float:
    return math.ceil(v / p - 1e-6) * p


def _support(pad, d, layer=None) -> float:
    """How far the pad's copper reaches from the pad position along the unit direction ``d`` (exact for
    boxes: support function of a rounded rectangle; polygons by their vertices)."""
    best = None
    for l in (sorted(pad.shapes) if layer is None else (layer,)):
        for s in pad.shapes.get(l, ()):
            if s.kind == "box":
                cx, cy, ux, uy, ihw, ihh, r = s.g
                v = (cx - pad.x) * d[0] + (cy - pad.y) * d[1] + abs(d[0] * ux + d[1] * uy) * ihw \
                    + abs(d[1] * ux - d[0] * uy) * ihh + r
            else:
                v = max((x - pad.x) * d[0] + (y - pad.y) * d[1] for x, y in s.g)
            if best is None or v > best:
                best = v
    return best or 0.0


def _fp_pads(board) -> dict:
    m = getattr(board, "_v2_fp_pads", None)
    if m is None:
        m = {}
        for p in board.pads:
            if p.shapes and p.layers:
                m.setdefault(p.ref, []).append(p)
        board._v2_fp_pads = m
    return m


def _pad_names(board) -> dict:
    m = getattr(board, "_v2_pad_names", None)
    if m is None:
        m = {}
        for p in board.pads:
            m.setdefault(p.name, []).append(p)
        board._v2_pad_names = m
    return m


def _key(pad) -> tuple:
    return (pad.name, round(pad.x, 4), round(pad.y, 4))


def _find_pad(board, name, x, y):
    """The Pad a terminal names (pad numbers repeat on some footprints: the nearest one of that name)."""
    cands = _pad_names(board).get(name) or ()
    best, bd = None, None
    for p in cands:
        d = math.hypot(p.x - x, p.y - y)
        if bd is None or d < bd:
            best, bd = p, d
    if best is None:
        return None
    if bd <= 0.05:
        return best
    for shs in best.shapes.values():
        for s in shs:
            if s.dist_pt(x, y) <= 0:
                return best
    return None


# ── rows ──────────────────────────────────────────────────────────────────
class Row:
    """Pads of one footprint on a line, ordered along ``t``; ``n`` is the exit direction (unit vectors, board
    mm). ``grid`` rows are lines of a pad matrix (BGA): no ring fan, dog-bone vias only."""
    __slots__ = ("ref", "idx", "pads", "n", "t", "pitch", "pitch2", "pad_w", "out_len", "in_len", "layers", "grid",
                 "ambiguous", "smd")

    def __repr__(self):
        return (f"Row({self.ref}#{self.idx} {len(self.pads)} pads {self.pads[0].name}..{self.pads[-1].name} "
                f"pitch {self.pitch:.3f} n=({self.n[0]:.2f},{self.n[1]:.2f}){' grid' if self.grid else ''})")


def _cluster(items, tol=LINE_TOL):
    """[(value, payload)] -> [[payload, ...]] of values chained within ``tol`` (input order kept inside)."""
    out = []
    last = None
    for v, x in sorted(items, key=lambda it: it[0]):
        if last is None or v - last > tol:
            out.append([])
        out[-1].append(x)
        last = v
    return out


def footprint_rows(board, ref: str) -> list:
    """Pad rows of footprint ``ref`` (cached on the board). A row is >= 3 pads of the same extent along the
    line, with collinear centres, in the footprint's own axes; pads that sit on a row in both axes form a
    grid. The exposed pad of a QFN, mounting pads and 2-pad parts are in no row."""
    cache = getattr(board, "_v2_rows", None)
    if cache is None:
        cache = board._v2_rows = {}
    hit = cache.get(ref)
    if hit is not None:
        return hit[0]
    pads = _fp_pads(board).get(ref) or []
    rows, by_pad = [], {}
    cache[ref] = (rows, by_pad)
    if len(pads) < MIN_ROW:
        return rows
    # footprint axes: the pads' own box orientation modulo 90 degrees (most common), else the nearest pad pair
    angs = {}
    for p in pads:
        for l in sorted(p.shapes):
            for s in p.shapes[l]:
                if s.kind == "box" and (s.g[4] > 1e-9 or s.g[5] > 1e-9):
                    a = round(math.degrees(math.atan2(s.g[3], s.g[2])) % 90.0, 1) % 90.0
                    angs[a] = angs.get(a, 0) + 1
            break
    if angs:
        theta = min(angs, key=lambda a: (-angs[a], a))
    else:
        p0 = pads[0]
        near = min(pads[1:], key=lambda p: (round(math.hypot(p.x - p0.x, p.y - p0.y), 6), p.num, p.x, p.y))
        theta = math.degrees(math.atan2(near.y - p0.y, near.x - p0.x)) % 90.0
    th = math.radians(theta)
    u = (math.cos(th), math.sin(th))
    v = (-math.sin(th), math.cos(th))
    u = tuple(round(c, 12) for c in u)
    v = tuple(round(c, 12) for c in v)
    recs = []
    for p in pads:
        a = p.x * u[0] + p.y * u[1]
        b = p.x * v[0] + p.y * v[1]
        eu = _support(p, u) + _support(p, (-u[0], -u[1]))
        ev = _support(p, v) + _support(p, (-v[0], -v[1]))
        recs.append((p, a, b, eu, ev))
    amid = (min(r[1] for r in recs) + max(r[1] for r in recs)) / 2
    bmid = (min(r[2] for r in recs) + max(r[2] for r in recs)) / 2

    def lines(coord, ext, along):
        groups = {}
        for r in recs:
            groups.setdefault((r[0].layers, round(r[ext], 2)), []).append(r)
        out, size, pitch = [], {}, {}
        for gk in sorted(groups):
            for cl in _cluster([(r[coord], r) for r in groups[gk]]):
                out.append(cl)
                xs = sorted(r[along] for r in cl)
                steps = [b2 - b1 for b1, b2 in zip(xs, xs[1:]) if b2 - b1 > 1e-6]
                for r in cl:
                    size[id(r[0])] = len(cl)
                    pitch[id(r[0])] = min(steps) if steps else 0.0
        return out, size, pitch

    ulines, hn, hp = lines(2, 3, 1)     # along u: constant b, same extent along u
    vlines, vn, vp = lines(1, 4, 2)     # along v: constant a, same extent along v

    def kind(pad):
        """'u' / 'v': the pad is on a row along that axis; 'g': a pad matrix (both ways, similar pitch)."""
        H, V = hn.get(id(pad), 0), vn.get(id(pad), 0)
        if H >= MIN_ROW and V >= MIN_ROW:
            pu, pv = hp[id(pad)], vp[id(pad)]
            if pu > 0 and pv > 0 and max(pu, pv) <= 1.5 * min(pu, pv):
                return "g"
            return "u" if pu <= pv else "v"             # a multi-row connector: the row is the denser line
        if H >= MIN_ROW:
            return "u"
        return "v" if V >= MIN_ROW else ""

    def make(members, axis, normal, coord, mid, grid):
        members = sorted(members, key=lambda r: (r[coord], r[0].num, r[0].x, r[0].y))
        if len(members) < MIN_ROW:
            return
        row = Row()
        row.ref, row.idx, row.grid = ref, len(rows), grid
        row.pads = [r[0] for r in members]
        row.t = axis
        off = sum(r[3 - coord] for r in members) / len(members) - mid       # the line's offset from the centre
        row.ambiguous = abs(off) < 0.05
        sgn = -1.0 if off < -0.05 else 1.0
        row.n = (normal[0] * sgn, normal[1] * sgn)
        steps = [members[i + 1][coord] - members[i][coord] for i in range(len(members) - 1)]
        steps = [s for s in steps if s > 1e-6]
        row.pitch = min(steps) if steps else 0.0
        row.pitch2 = 0.0
        row.pad_w = max(r[3] if coord == 1 else r[4] for r in members)
        row.out_len = max(_support(p, row.n) for p in row.pads)
        row.in_len = max(_support(p, (-row.n[0], -row.n[1])) for p in row.pads)
        row.layers = row.pads[0].layers
        row.smd = all(p.kind in SMD_KINDS for p in row.pads)
        rows.append(row)
        for p in row.pads:
            by_pad[id(p)] = row

    gridlines = []
    for ln in ulines:
        if len(ln) < MIN_ROW:
            continue
        g = [r for r in ln if kind(r[0]) == "g"]
        if len(g) >= MIN_ROW:
            gridlines.append(g)
        make([r for r in ln if kind(r[0]) == "u"], u, v, 1, bmid, False)
    for ln in vlines:
        if len(ln) < MIN_ROW:
            continue
        make([r for r in ln if kind(r[0]) == "v"], v, u, 2, amid, False)
    # an inner row of a multi-row connector: "outwards" is where the next parallel row is further away (the
    # outer row stands in the way on the other side)
    for row in rows:
        free_out = free_in = float("inf")
        for o in rows:
            if o is row or abs(o.t[0] * row.t[0] + o.t[1] * row.t[1]) < 0.99:
                continue
            d = (o.pads[0].x - row.pads[0].x) * row.n[0] + (o.pads[0].y - row.pads[0].y) * row.n[1]
            if d > LINE_TOL:
                free_out = min(free_out, d)
            elif d < -LINE_TOL:
                free_in = min(free_in, -d)
        if free_out < free_in and not row.ambiguous:
            row.n = (-row.n[0], -row.n[1])
            row.out_len, row.in_len = row.in_len, row.out_len
    bs = sorted(sum(r[2] for r in g) / len(g) for g in gridlines)
    p2 = min((b2 - b1 for b1, b2 in zip(bs, bs[1:]) if b2 - b1 > 1e-6), default=0.0)
    for g in gridlines:
        n0 = len(rows)
        make(g, u, v, 1, bmid, True)
        if len(rows) > n0:
            rows[-1].pitch2 = p2 or rows[-1].pitch
    return rows


def _row_of(board, pad):
    footprint_rows(board, pad.ref)
    return board._v2_rows[pad.ref][1].get(id(pad))


def needs_fanout(board, ref: str, w: float, cl: float) -> bool:
    """Design §4.5: a footprint with a pad row whose ``pitch < 2 x (w + cl) + pad_w`` (two tracks do not fit
    between neighbouring pads), or with >= 16 pads. Footprints without any pad row (2-pad parts, lone pads)
    never need one: there is no row to order."""
    rows = footprint_rows(board, ref)
    if not rows:
        return False
    if len(_fp_pads(board).get(ref) or ()) >= 16:
        return True
    return any(r.pitch < 2 * (w + cl) + r.pad_w - 1e-9 for r in rows)


# ── registry ──────────────────────────────────────────────────────────────
_FALLBACK = {}


def registry(ctx: Ctx) -> dict:
    """The fan-out book of this run: ``{'by_pad': {key: record}, 'hist': {key: [every record the pad ever
    had, oldest first]}, 'boxed': {key: fail}, 'normals': {...}}`` with ``key = (pad name, x, y)``. A record is a dict: pad, ref, net, kind
    ('ring' | 'via'), pieces (ids), terminal, orig {(conn idx, end): Terminal}, desc, width, layer, lane."""
    reg = getattr(ctx, "escapes", None)
    if reg is None:
        reg = {"by_pad": {}, "hist": {}, "boxed": {}, "normals": {}}
        try:
            ctx.escapes = reg
        except AttributeError:
            reg = _FALLBACK.setdefault(id(ctx), reg)
    return reg


def escape_of(ctx: Ctx, pad: str, x: float | None = None, y: float | None = None) -> dict | None:
    """The current escape record of a pad ('U3.4'; x, y pick one of several pads sharing a name), or None."""
    best, bd = None, None
    for k in sorted(registry(ctx)["by_pad"]):
        if k[0] != pad:
            continue
        d = 0.0 if x is None else math.hypot(k[1] - x, k[2] - y)
        if bd is None or d < bd:
            best, bd = registry(ctx)["by_pad"][k], d
    return best


def _conn_by_idx(ctx) -> dict:
    return {c.idx: c for c in ctx.conns}


def _drop(ctx, rec, remove_pieces=True) -> None:
    """Forget an escape: terminals back to the pad, pieces out of the state."""
    conns = _conn_by_idx(ctx)
    for (ci, end), term in sorted(rec["orig"].items()):
        c = conns.get(ci)
        if c is not None:
            setattr(c, end, term)
    if remove_pieces:
        for pid in rec["pieces"]:
            if ctx.oracle.has_piece(pid):
                ctx.state.remove(pid)


def sync(ctx: Ctx) -> int:
    """Re-align the book with ``ctx.state`` (call after ``state.restore`` / a rollback that crossed a fan-out
    change / removing ``stage='fanout'`` pieces). For every pad that ever had an escape the current one becomes
    the newest whose pieces are all in the state (so a restore to an earlier or a later snapshot both work);
    with none, the pad's connections get their pad terminals back. Cost: one dictionary look-up per escape
    piece. Returns the number of pads changed."""
    reg = registry(ctx)
    has = ctx.oracle.has_piece
    by_pad = reg["by_pad"]
    todo = []
    for key, chain in reg["hist"].items():
        cur = by_pad.get(key)
        good = None
        for rec in reversed(chain):
            for pid in rec["pieces"]:
                if not has(pid):
                    break
            else:
                good = rec
                break
        if good is not cur:
            todo.append((key, cur, good))
    if not todo:
        return 0
    conns = _conn_by_idx(ctx)
    for key, cur, good in sorted(todo, key=lambda t: t[0]):
        src = good if good is not None else cur
        for (ci, end), orig in sorted(src["orig"].items()):
            c = conns.get(ci)
            if c is not None:
                setattr(c, end, orig if good is None else good["terminal"])
        if good is None:
            del by_pad[key]
        else:
            by_pad[key] = good
    return len(todo)


HOOK_STATE = True            # run() wraps ctx.state.restore so the book follows snapshot restores (hook_state)


def hook_state(ctx: Ctx) -> bool:
    """Wrap ``ctx.state.restore`` (on this instance only) so that ``sync(ctx)`` runs after every snapshot
    restore: the engine's keep-best gate and the judge restore snapshots without knowing about escape
    terminals. Idempotent; off with ``fanout.HOOK_STATE = False`` (then the caller calls ``sync`` itself).
    Rollbacks are not hooked: whoever rolls back across ``re_escape`` calls ``sync`` (repair does)."""
    st = ctx.state
    if not HOOK_STATE or st is None or getattr(st, "_fanout_hooked", False):
        return False
    try:
        inner = st.restore

        def restore(snap, _inner=inner, _ctx=ctx):
            r = _inner(snap)
            if getattr(_ctx, "escapes", None) is not None and _ctx.state is st:
                sync(_ctx)
            return r

        st.restore = restore
        st._fanout_hooked = True
    except (AttributeError, TypeError):
        return False                                        # a state that cannot be wrapped: sync() is the caller's job
    return True


def undo(ctx: Ctx) -> int:
    """Remove every escape (pieces and escape terminals). Returns the number of pads undone."""
    reg = registry(ctx)
    n = 0
    for key in sorted(reg["by_pad"]):
        _drop(ctx, reg["by_pad"][key])
        n += 1
    reg["by_pad"].clear()
    reg["hist"].clear()
    return n


# ── jobs: one pad that wants an escape ────────────────────────────────────
class _Job:
    __slots__ = ("pad", "key", "net", "conns", "width", "neck", "cl", "clr", "via", "layer", "partner", "offlayer",
                 "near", "has_copper", "options", "all_options", "result", "blocker")


def _opt(ctx, ref) -> tuple:
    """-> ({'style', 'side', 'layer'}, explicit). style: out | in | via | auto | none."""
    x = ctx.x
    parts = getattr(x, "fanout_parts", None) or {}
    if ref in parts:
        d = parts[ref] or {}
        return {"style": d.get("style") or "auto", "side": d.get("side"), "layer": d.get("layer")}, True
    mode = getattr(x, "fanout", "auto")
    if isinstance(mode, dict):                              # a V2Cfg that keeps the per-part table under .fanout
        if ref in mode:
            d = mode[ref] or {}
            return {"style": d.get("style") or "auto", "side": d.get("side"), "layer": d.get("layer")}, True
        mode = "auto"
    return {"style": "none" if mode == "none" else "auto", "side": None, "layer": None}, False


def _status(ctx, conn) -> str:
    try:
        return ctx.state.status(conn.idx)
    except Exception:  # noqa: BLE001  (a state that does not know the connection yet)
        return "todo"


def _jobs(ctx: Ctx) -> dict:
    """key -> _Job for every pad that ends a connection still to route (not a plane connection, not already
    an escape terminal). During ``run`` the table is built once (pads escaped since stay in it; the book says
    which those are)."""
    cached = registry(ctx).get("_jobs")
    if cached is not None:
        return cached
    board, orc = ctx.board, ctx.oracle
    jobs = {}
    for c in sorted(ctx.conns, key=lambda c: (c.rank, c.idx)):
        if c.role == "plane" or _status(ctx, c) != "todo":
            continue
        for end, other in (("a", "b"), ("b", "a")):
            term = getattr(c, end)
            if term.pad is None or term.escape:
                continue
            pad = _find_pad(board, term.pad, term.x, term.y)
            if pad is None or not pad.net:
                continue
            key = _key(pad)
            j = jobs.get(key)
            if j is None:
                j = jobs[key] = _Job()
                net = board.net_by_id[pad.net]
                j.pad, j.key, j.net, j.conns = pad, key, net.name, []
                j.width = c.width or net.width
                j.neck = min(c.neck or net.width, j.width)
                j.cl = c.clearance or 0.0
                j.clr = max(orc.clearance(net.name), j.cl)
                j.via = tuple(c.via) if c.via and c.via[0] > 0 else (net.via, net.drill)
                j.layer = pad.layers[0]
                want = _opt(ctx, pad.ref)[0].get("layer")
                if want is not None and want in pad.layers:
                    j.layer = want
                _layer_width(ctx, j)
                o = getattr(c, other)
                j.partner = (o.x, o.y)
                j.offlayer = True
                j.near = math.hypot(o.x - pad.x, o.y - pad.y)
                j.has_copper = False
                j.options, j.all_options, j.result, j.blocker = [], [], None, None
            o = getattr(c, other)
            if set(o.layers) & set(pad.layers):
                j.offlayer = False
            j.near = min(j.near, math.hypot(o.x - pad.x, o.y - pad.y))
            j.conns.append((c, end))
    if jobs:
        # pads that already carry copper of their net (a power stub, kept routing): nothing to escape
        for key in sorted(jobs):
            j = jobs[key]
            for p in ctx.state.pieces(net=j.net):
                if (abs(p.x1 - j.pad.x) < 1e-3 and abs(p.y1 - j.pad.y) < 1e-3) or \
                        (abs(p.x2 - j.pad.x) < 1e-3 and abs(p.y2 - j.pad.y) < 1e-3):
                    j.has_copper = True
                    break
    return jobs


def _carg(j):
    return j.cl if j.cl > 0 else None


def _layer_width(ctx, j) -> None:
    """A net with a per-layer width rule (design §14.8): the escape is laid at the width of the pad's own layer
    (the rule's base width on a layer it does not name), not at the widest of the net's layers."""
    cons = getattr(getattr(ctx, "x", None), "cons", None)
    if cons is None or not hasattr(cons, "lay_width"):
        return
    w = cons.lay_width(j.net, j.layer, j.width)
    if abs(w - j.width) > 1e-9:
        j.width = q(w)
        j.neck = min(j.neck, j.width)


def _widths(j) -> list:
    return [j.width] if j.neck >= j.width - 1e-9 else [j.width, j.neck]


# ── row geometry shared by every call on a row ────────────────────────────
class _Geo:
    __slots__ = ("n", "t", "p", "rs", "R", "unit")


def _row_normal(ctx, row, jobs, opt) -> tuple:
    """Exit direction of a row; for a single-row part (no outward side) the plan's side, else the side most
    partners are on (kept for the whole run)."""
    if not row.ambiguous:
        return row.n
    reg = registry(ctx)
    k = (row.ref, row.idx)
    if k in reg["normals"]:
        return reg["normals"][k]
    n = row.n
    side = SIDE_VEC.get(opt.get("side") or "")
    if side is not None and abs(side[0] * n[0] + side[1] * n[1]) > 0.5:
        s = side[0] * n[0] + side[1] * n[1]
    else:
        s = 0.0
        for p in row.pads:
            j = jobs.get(_key(p))
            if j is not None:
                f = (j.partner[0] - p.x) * n[0] + (j.partner[1] - p.y) * n[1]
                s += (f > 1e-6) - (f < -1e-6)
    if s < 0:
        n = (-n[0], -n[1])
    reg["normals"][k] = n
    return n


def _row_geo(ctx, row, jobs, opt) -> _Geo:
    """Rings and lattice of a row, from every connected pad on it (escaped or not), so repeated calls agree."""
    reg = registry(ctx)
    ws, cls = [], []
    for p in row.pads:
        k = _key(p)
        j = jobs.get(k)
        rec = reg["by_pad"].get(k)
        if j is not None:
            ws.append(j.width)
            cls.append(j.clr)
        elif rec is not None:
            ws.append(rec["full_width"])
            cls.append(rec["clr"])
    if not ws:
        net_w = ctx.board.rules["default"].get("width") or 0.25
        ws, cls = [net_w], [ctx.board.default_clr]
    # the row's usual track (the most common w + cl, the smaller on a tie) sets the rings: one wide power pin
    # must not push every slot of the row outwards; lanes are still judged pair by pair with the real widths
    count = {}
    for w, c in zip(ws, cls):
        k = (round(w + c, 6), w, c)
        count[k] = count.get(k, 0) + 1
    unit, w0, c0 = min(count, key=lambda k: (-count[k], k))
    g = _Geo()
    g.n = _row_normal(ctx, row, jobs, opt)
    g.t = row.t
    g.unit = unit
    g.p = lattice_pitch(row.pitch, w0, c0)
    g.rs = _snap_up(unit, g.p)
    out_len = row.out_len if g.n == row.n else row.in_len
    r1 = _snap_up(out_len + unit, g.p)
    g.R = [r1 + k * g.rs for k in range(RINGS)]
    return g


# ── lanes: the planar order of a row's slots ──────────────────────────────
def _maxh(F, lo, hi, rs) -> float:
    """Highest ring level of escape F's copper inside the lateral window [lo, hi] (-1: none there)."""
    h = -1.0
    tf = F["t"]
    if lo <= tf <= hi:
        h = float(F["a"])
    if F["b"] > F["a"]:
        l0, l1 = (tf, F["lane"]) if tf <= F["lane"] else (F["lane"], tf)
        i0, i1 = max(lo, l0), min(hi, l1)
        if i0 <= i1:
            far = i0 if abs(i0 - tf) > abs(i1 - tf) else i1
            h = max(h, F["a"] + abs(far - tf) / rs)
    return h


def _blocks(E, F, rs) -> bool:
    """Does escape F's copper close the outward lane of E (the ray from E's slot along the row normal)?"""
    sp = (E["w"] + F["w"]) / 2 + max(E["cl"], F["cl"])
    if not E["real"]:
        # a pad without an escape: only a leg heading across its exit counts (its neighbours' stubs are where
        # the footprint put them)
        if F["b"] <= F["a"]:
            return False
        d = F["lane"] - F["t"]
        off = E["t"] - F["t"]
        return off * d > 0 and abs(off) < abs(d) + sp - LANE_TOL
    return _maxh(F, E["lane"] - sp + LANE_TOL, E["lane"] + sp - LANE_TOL, rs) >= E["b"] - 1e-6


def _lane_ok(E, others, rs) -> bool:
    """Slot order equals pad order, and no escape closes another one's outward lane."""
    for F in others:
        if (E["lane"] - F["lane"]) * (E["t"] - F["t"]) <= 0 and abs(E["t"] - F["t"]) > LANE_TOL:
            return False
        if _blocks(E, F, rs) or _blocks(F, E, rs):
            return False
    return True


def _tco(geo, pad) -> float:
    return pad.x * geo.t[0] + pad.y * geo.t[1]


def _row_lanes(ctx, row, geo, jobs, skip_key=None) -> list:
    """Lane entries of every connected pad of the row: real ones from the book, reserved ones (level 0) for
    connected pads without an escape."""
    reg = registry(ctx)
    out = []
    for p in row.pads:
        k = _key(p)
        if k == skip_key:
            continue
        rec = reg["by_pad"].get(k)
        if rec is not None and rec.get("lane") is not None:
            out.append(rec["lane"])
        elif rec is None and k in jobs:
            j = jobs[k]
            tc = _tco(geo, p)
            out.append({"t": tc, "lane": tc, "a": 0, "b": 0, "w": j.neck, "cl": j.clr, "real": False})
    return out


# ── options: one candidate escape, built analytically, judged by the oracle ──
def _ring_option(job, geo, a, b, d, w) -> dict:
    pad = job.pad
    n, t = geo.n, geo.t
    x0, y0 = q(pad.x), q(pad.y)
    ra = geo.R[a - 1]
    x1, y1 = q(pad.x + n[0] * ra), q(pad.y + n[1] * ra)
    segs = [(x0, y0, x1, y1)]
    sx, sy = x1, y1
    sh = 0.0
    if b > a:
        sh = geo.R[b - 1] - ra
        sx, sy = q(pad.x + n[0] * (ra + sh) + d * t[0] * sh), q(pad.y + n[1] * (ra + sh) + d * t[1] * sh)
        segs.append((x1, y1, sx, sy))
    tc = _tco(geo, pad)
    return {"kind": "ring", "segs": segs, "via": None, "w": w, "end": (sx, sy), "layers": (job.layer,),
            "desc": {"ring": b, "from": a, "dir": d if b > a else 0},
            "lane": {"t": tc, "lane": tc + d * sh, "a": a, "b": b, "w": w, "cl": job.clr, "real": True},
            "cost": ra + sh * math.sqrt(2.0), "soft": 0}


def _check(ctx, job, o, ignore=frozenset(), probe=None) -> Blocker | str | None:
    """None = the option is legal copper (and, for ring options, leaves a way on); else the Blocker or a
    reason. Sets ``o['soft']`` to the number of thermal spokes crossed."""
    orc = ctx.oracle
    layer = job.layer
    ca = _carg(job)
    soft = 0
    for x1, y1, x2, y2 in o["segs"]:
        blk = orc.seg_ok(layer, x1, y1, x2, y2, o["w"], job.net, ca, ignore)
        if blk is not None:
            return blk
        for h in orc.soft_hits(layer, x1, y1, x2, y2, o["w"], job.net):
            if h.rule == "reserve":
                return h
            soft += 1
    if o["via"] is not None:
        vx, vy = o["via"]
        size, drill = job.via
        blk = orc.via_ok(vx, vy, size, drill, job.net, ignore, ca)
        if blk is not None:
            return blk
        if _in_smd_pad(ctx, vx, vy, size / 2):
            return "via in pad"
        for l in range(ctx.board.L):
            for h in orc.soft_hits(l, vx, vy, vx, vy, size, job.net):
                if h.rule == "reserve":
                    return h
                soft += 1
    o["soft"] = soft
    if probe is not None:
        # a way on from the slot: straight ahead or one of the two outward diagonals
        sx, sy = o["end"]
        n, t, ln = probe
        for dx, dy in ((n[0], n[1]), (n[0] + t[0], n[1] + t[1]), (n[0] - t[0], n[1] - t[1])):
            if orc.seg_legal(layer, sx, sy, q(sx + dx * ln), q(sy + dy * ln), o["w"], job.net, ca, ignore):
                return None
        return orc.seg_ok(layer, sx, sy, q(sx + n[0] * ln), q(sy + n[1] * ln), o["w"], job.net, ca, ignore) \
            or "dead end"
    return None


def _in_smd_pad(ctx, x, y, vr) -> bool:
    """v1's no-via-in-pad rule: a via may touch an SMD pad of its net but not sit on it."""
    lim = vr * 0.6
    for l in (0, ctx.board.L - 1):
        for s in ctx.board.index.near(l, (x, y, x, y), lim):
            tag = s.tag
            if s.cls == "cu" and tag is not None and getattr(tag, "kind", None) in SMD_KINDS and s.dist_pt(x, y) < lim:
                return True
    return False


def _place(ctx, job, o, ignore_rec=None) -> dict:
    """Commit one option: protected fan-out pieces (conn = -1), escape terminals, the book."""
    st = ctx.state
    reg = registry(ctx)
    tok = st.begin()
    if ignore_rec is not None:
        for pid in ignore_rec["pieces"]:
            if ctx.oracle.has_piece(pid):
                st.remove(pid)
    pieces = []
    for x1, y1, x2, y2 in o["segs"]:
        if x1 == x2 and y1 == y2:
            continue
        pieces.append(st.add(net=job.net, conn=-1, kind="seg", layer=job.layer, x1=x1, y1=y1, x2=x2, y2=y2,
                             w=q(o["w"]), stage="fanout", protected=True, target_w=q(job.width)))
    if o["via"] is not None:
        vx, vy = o["via"]
        pieces.append(st.add(net=job.net, conn=-1, kind="via", layer=-1, x1=vx, y1=vy, x2=vx, y2=vy,
                             w=q(job.via[0]), drill=q(job.via[1]), stage="fanout", protected=True))
        _book_via(ctx, vx, vy)
    st.commit(tok)
    first = job.conns[0][0]
    term0 = getattr(first, job.conns[0][1])
    orig = dict(ignore_rec["orig"]) if ignore_rec is not None else {}
    for c, end in job.conns:
        orig.setdefault((c.idx, end), getattr(c, end))
    cluster = next(iter(orig.values())).cluster if orig else term0.cluster
    term = Terminal(x=o["end"][0], y=o["end"][1], layers=tuple(o["layers"]), pad=job.pad.name, cluster=cluster,
                    escape=True)
    conns = _conn_by_idx(ctx)
    for (ci, end) in sorted(orig):
        c = conns.get(ci)
        if c is not None:
            setattr(c, end, term)
    rec = {"key": job.key, "pad": job.pad.name, "ref": job.pad.ref, "net": job.net, "kind": o["kind"],
           "pieces": [p.id for p in pieces], "terminal": term, "orig": orig, "desc": dict(o["desc"]),
           "width": o["w"], "full_width": job.width, "clr": job.clr, "layer": job.layer, "lane": o.get("lane"),
           "length": round(sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in o["segs"]), 6), "soft": o["soft"],
           "via": o["via"]}
    reg["hist"].setdefault(job.key, []).append(rec)
    reg["by_pad"][job.key] = rec
    reg["boxed"].pop(job.key, None)
    return {"pad": job.pad.name, "ok": True, "kind": o["kind"], "pieces": pieces, "terminal": term,
            "desc": dict(o["desc"]), "width": o["w"]}


def _tiles(ctx):
    t = ctx.tiles
    if t is None or getattr(t, "STUB", False):
        return None
    return t


def _book_via(ctx, x, y) -> None:
    t = _tiles(ctx)
    if t is None:
        return
    try:
        tx, ty = t.tile_of(x, y)
        t.book_via(tx, ty, 1)
    except Exception:  # noqa: BLE001  (tiles are optional for this stage)
        pass


def _congestion(ctx, x, y, unit) -> float:
    """Cost (mm) of using a via site: scarce via columns are dearer (0 without a tile graph)."""
    t = _tiles(ctx)
    if t is None:
        return 0.0
    try:
        vc = t.via_cap(*t.tile_of(x, y))
    except Exception:  # noqa: BLE001
        return 0.0
    return unit * (2.0 if vc <= 0 else 1.0 / vc)


def _fail(job, reason, blocker=None) -> dict:
    blocked = []
    where = [round(job.pad.x, 4), round(job.pad.y, 4)]
    if isinstance(blocker, Blocker):
        blocked = [blocker.tag or blocker.rule]
        where = [blocker.at[0], blocker.at[1]]
    elif isinstance(blocker, str):
        reason = f"{reason} ({blocker})" if blocker not in reason else reason
    return {"pad": job.pad.name, "ok": False, "fail": {"reason": reason, "blocked_by": blocked, "where": where}}


# ── ring fan ──────────────────────────────────────────────────────────────
def _as_pads(board, ref, row) -> list:
    out = []
    for it in row:
        if isinstance(it, str):
            name = it if "." in it or it == ref else f"{ref}.{it}"
            out += [p for p in _pad_names(board).get(name) or () if p.ref == ref]
        else:
            out.append(it)
    return out


def _lean(job, geo, opt) -> int:
    """-1 / +1: the row direction the escape should lean to (bearing of the partner, or the plan's side);
    0: the partner is straight ahead, keep the straight stub."""
    side = SIDE_VEC.get(opt.get("side") or "")
    if side is not None:
        s = side[0] * geo.t[0] + side[1] * geo.t[1]
        if abs(s) > 0.5:
            return 1 if s > 0 else -1
    vx, vy = job.partner[0] - job.pad.x, job.partner[1] - job.pad.y
    fwd = vx * geo.n[0] + vy * geo.n[1]
    lat = vx * geo.t[0] + vy * geo.t[1]
    if abs(lat) < geo.rs or abs(lat) <= 0.41421356 * max(fwd, 0.0) + 1e-9:
        return 0
    return 1 if lat > 0 else -1


def _try_ring(ctx, job, geo, lanes, cands, ignore=frozenset()):
    """First clean legal candidate of ``cands`` [(a, b, d)], else the first legal one that crosses a thermal
    spoke, else (None, last blocker)."""
    probe = (geo.n, geo.t, geo.rs)
    fallback = None
    last = None
    for a, b, d in cands:
        for w in _widths(job):
            o = _ring_option(job, geo, a, b, d, w)
            if not _lane_ok(o["lane"], lanes, geo.rs):
                last = last or "lane taken"
                continue                                # a thinner stub needs a narrower lane
            r = _check(ctx, job, o, ignore, probe)
            if r is None:
                if o["soft"] == 0:
                    return o, None
                fallback = fallback or o
                break
            if last is None or isinstance(last, str):
                last = r
    return fallback, last


def _fan_row(ctx, row, pads, jobs, opt) -> dict:
    """Ring fan of the requested pads of one row -> {key: result}."""
    out = {}
    geo = _row_geo(ctx, row, jobs, opt)
    short = geo.R[0] + 2 * geo.rs
    active = []
    for p in pads:
        k = _key(p)
        j = jobs.get(k)
        if j is None:
            out[k] = {"pad": p.name, "ok": False, "skip": "no connection to route from this pad"}
        elif k in registry(ctx)["by_pad"]:
            out[k] = {"pad": p.name, "ok": False, "skip": "pad already has an escape"}
        elif j.has_copper:
            out[k] = {"pad": p.name, "ok": False, "skip": "pad already has copper"}
        elif j.near < short:
            out[k] = {"pad": p.name, "ok": False, "skip": "partner next to the pad"}
        else:
            active.append(j)
    active.sort(key=lambda j: (_tco(geo, j.pad), j.key))
    placed = {}                                 # key -> option
    # pass 1: the straight stub to the first ring, in pad order
    failed = []
    for j in active:
        o, why = _try_ring(ctx, j, geo, _row_lanes(ctx, row, geo, jobs, j.key), [(1, 1, 0)])
        if o is None:
            j.blocker = why
            failed.append(j)
        else:
            out[j.key] = _place(ctx, j, o)
            placed[j.key] = o
    # pass 2a: pads without a straight exit try a leg, towards the partner first
    for j in failed:
        d0 = _lean(j, geo, opt) or 1
        cands = [(a, b, d) for d in (d0, -d0) for a, b in ((1, 2), (1, 3), (2, 3))]
        o, why = _try_ring(ctx, j, geo, _row_lanes(ctx, row, geo, jobs, j.key), cands)
        if o is None:
            out[j.key] = _fail(j, "no legal ring escape", j.blocker or why)
        else:
            out[j.key] = _place(ctx, j, o)
            placed[j.key] = o
    # pass 2b: pads whose partner is off to one side lean their slot that way when a lane is free; the pad
    # nearest the row end the fan heads to goes first
    lean = [(j, _lean(j, geo, opt)) for j in active if j.key in placed and placed[j.key]["desc"]["ring"] == 1]         if LEAN_TO_PARTNER or SIDE_VEC.get(opt.get("side") or "") is not None else []
    order = sorted((jd for jd in lean if jd[1] < 0), key=lambda jd: (_tco(geo, jd[0].pad), jd[0].key)) + \
        sorted((jd for jd in lean if jd[1] > 0), key=lambda jd: (-_tco(geo, jd[0].pad), jd[0].key))
    reg = registry(ctx)
    for j, d in order:
        rec = reg["by_pad"].get(j.key)
        if rec is None:
            continue
        o, _why = _try_ring(ctx, j, geo, _row_lanes(ctx, row, geo, jobs, j.key),
                            [(1, 3, d), (2, 3, d), (1, 2, d)], frozenset(rec["pieces"]))
        if o is not None and o["soft"] <= rec["soft"]:
            out[j.key] = _place(ctx, j, o, ignore_rec=rec)
            placed[j.key] = o
    return out


def ring_fan(ctx: Ctx, ref: str, row: list, style: str = "auto") -> list[dict]:
    """Escape the pads ``row`` (Pad objects, pad names 'U1.3' or pad numbers) of footprint ``ref`` on their
    own layer: straight stubs to the first ring, legs to slots where a lane is free. ``style`` 'in' / 'via'
    hands the pads to ``via_escape``.

    Returns one dict per pad, in input order:
      {'pad', 'ok': True, 'kind': 'ring', 'pieces': [Piece], 'terminal': Terminal, 'desc': {ring, from, dir}, 'width'}
      {'pad', 'ok': False, 'fail': {'reason', 'blocked_by', 'where'}}      no legal ring escape
      {'pad', 'ok': False, 'skip': '...'}                                  nothing to do for this pad
    Placed escapes are in ``ctx.state`` (protected, stage 'fanout') and the connections' terminals are moved."""
    board = ctx.board
    pads = _as_pads(board, ref, row)
    if not pads:
        return []
    if style in ("in", "via"):
        return via_escape(ctx, ref, pads)
    jobs = _jobs(ctx)
    opt, _explicit = _opt(ctx, ref)
    groups, order, out = {}, [], {}
    for p in pads:
        r = _row_of(board, p)
        if r is None or r.grid:
            j = jobs.get(_key(p))
            out[_key(p)] = _fail(j, "not on a pad row") if j is not None else \
                {"pad": p.name, "ok": False, "skip": "no connection to route from this pad"}
            continue
        if r.idx not in groups:
            groups[r.idx] = (r, [])
            order.append(r.idx)
        groups[r.idx][1].append(p)
    for i in order:
        r, plist = groups[i]
        out.update(_fan_row(ctx, r, plist, jobs, opt))
    return [out[_key(p)] for p in pads]


# ── via escape ────────────────────────────────────────────────────────────
def _via_options(ctx, job, row, geo, opt) -> list:
    """Candidate (stub, via) pairs of one pad in generation order, unchecked. Row pads: outwards and inwards,
    3 depths, straight or with a 45 degree leg to either side. Grid pads: the 4 interstitial sites. Lone pads:
    8 directions, 2 depths."""
    pad = job.pad
    size, _drill = job.via
    vr = size / 2
    cl = job.clr
    style = opt.get("style") or "auto"
    side = SIDE_VEC.get(opt.get("side") or "")
    layers = tuple(range(ctx.board.L)) if opt.get("layer") is None else (opt["layer"],)
    unit = job.width + cl
    out = []
    x0, y0 = q(pad.x), q(pad.y)

    def add(segs, via, tag):
        ln = sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in segs)
        cost = ln + _congestion(ctx, via[0], via[1], unit)
        if side is not None and (via[0] - pad.x) * side[0] + (via[1] - pad.y) * side[1] <= 0:
            cost += 4 * unit                                # the plan wants the escapes to head the other way
        out.append({"kind": "via", "segs": segs, "via": via, "w": job.width, "end": via, "layers": layers,
                    "desc": tag, "lane": None, "cost": cost, "soft": 0, "seq": len(out)})

    if row is not None and row.grid:
        hx, hy = row.pitch / 2, (row.pitch2 or row.pitch) / 2
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            vx = q(pad.x + sx * hx * row.t[0] + sy * hy * row.n[0])
            vy = q(pad.y + sx * hx * row.t[1] + sy * hy * row.n[1])
            add([(x0, y0, vx, vy)], (vx, vy), {"dir": "dogbone", "depth": 1, "shift": sx, "side": sy})
        return out
    if row is not None:
        p = geo.p
        lv = _snap_up(size + cl, p)
        dl = _snap_up(job.width + cl, p)
        dirs = []
        if style != "in":
            dirs.append(("out", geo.n))
        if style != "out":
            dirs.append(("in", (-geo.n[0], -geo.n[1])))
        t = geo.t
        for name, d in dirs:
            base = _snap_up(_support(pad, d, job.layer) + cl + vr, p)
            for depth in range(1, VIA_DEPTHS + 1):
                dist = base + (depth - 1) * lv
                for m in (0, -1, 1):
                    if m == 0:
                        vx, vy = q(pad.x + d[0] * dist), q(pad.y + d[1] * dist)
                        segs = [(x0, y0, vx, vy)]
                    else:
                        bend = dist - dl
                        if bend < -1e-9:
                            continue
                        bx, by = q(pad.x + d[0] * bend), q(pad.y + d[1] * bend)
                        vx, vy = q(pad.x + d[0] * dist + m * t[0] * dl), q(pad.y + d[1] * dist + m * t[1] * dl)
                        segs = [(x0, y0, bx, by), (bx, by, vx, vy)]
                    add(segs, (vx, vy), {"dir": name, "depth": depth, "shift": m})
        return out
    # a pad on no row: dog-bone candidates around it, along the pad's own axes and their diagonals
    ux, uy = 1.0, 0.0
    for s in pad.shapes.get(job.layer, ()):
        if s.kind == "box":
            ux, uy = s.g[2], s.g[3]
            break
    p = lattice_pitch(0.0, job.width, cl)
    lv = _snap_up(size + cl, p)
    for i, (dx, dy) in enumerate(((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))):
        k = 1.0 if i < 4 else _S2
        d = ((dx * ux - dy * uy) * k, (dx * uy + dy * ux) * k)
        base = _snap_up(_support(pad, d, job.layer) + cl + vr, p)
        for depth in (1, 2):
            dist = base + (depth - 1) * lv
            vx, vy = q(pad.x + d[0] * dist), q(pad.y + d[1] * dist)
            add([(x0, y0, vx, vy)], (vx, vy), {"dir": f"d{i}", "depth": depth, "shift": 0})
    return out


def _legal_width(ctx, job, o, ignore=frozenset()):
    """Check a via option at the full width, then at the neck width. -> None when legal (o['w'] set) else the
    blocker of the full-width attempt."""
    first = None
    for w in _widths(job):
        o["w"] = w
        r = _check(ctx, job, o, ignore)
        if r is None:
            return None
        if first is None:
            first = r
    return first


def _local_cut(ctx, a: str, b: str, layer: int, w: float, cl: float, nets=()) -> Cut:
    """The pad-pair cut between two pads: Tiles.pad_cut when a tile graph exists, else the same formula
    (design §4.6: tracks = floor((gap - cl) / (w + cl)))."""
    t = _tiles(ctx)
    if t is not None:
        try:
            c = t.pad_cut(a, b, layer, w, cl)
            if isinstance(c, Cut):
                return Cut(a=c.a, b=c.b, layer=c.layer, capacity_mm=round(c.capacity_mm, 4),
                           demand_mm=c.demand_mm or round(w + cl, 4), nets=list(c.nets or nets))
        except Exception:  # noqa: BLE001
            pass
    try:
        gap = ctx.oracle.pad_gap(a, b, layer)
    except KeyError:
        gap = float("inf")
    if gap == float("inf"):
        tracks = 0
    else:
        tracks = max(0, int(math.floor((gap - cl) / (w + cl) + 1e-9)))
    return Cut(a=a, b=b, layer=ctx.board.layers[layer], capacity_mm=round(tracks * (w + cl), 4),
               demand_mm=round(w + cl, 4), nets=list(nets))


def _pad_pairs(board, ref) -> list:
    """Neighbouring pad pairs of a footprint's rows: (x0, y0, x1, y1 of the pair's box, pad a, pad b, layers)."""
    cache = getattr(board, "_v2_pairs", None)
    if cache is None:
        cache = board._v2_pairs = {}
    out = cache.get(ref)
    if out is None:
        out = cache[ref] = []
        for row in footprint_rows(board, ref):
            ps = row.pads
            for i in range(len(ps) - 1):
                pa, pb = ps[i], ps[i + 1]
                out.append((min(pa.x, pb.x), min(pa.y, pb.y), max(pa.x, pb.x), max(pa.y, pb.y), pa, pb, row.layers))
    return out


def _crossed_cut(ctx, job, o):
    """The neighbouring pad pair of the pad's own footprint a stub passes between, or None."""
    pad = job.pad
    layer = job.layer
    pairs = _pad_pairs(ctx.board, pad.ref)
    for x1, y1, x2, y2 in o["segs"]:
        sx0, sx1 = (x1, x2) if x1 <= x2 else (x2, x1)
        sy0, sy1 = (y1, y2) if y1 <= y2 else (y2, y1)
        for bx0, by0, bx1, by1, pa, pb, layers in pairs:
            if bx0 > sx1 or bx1 < sx0 or by0 > sy1 or by1 < sy0 or pa is pad or pb is pad or layer not in layers:
                continue
            # proper crossing of the stub and the line between the two pad centres
            dx, dy = x2 - x1, y2 - y1
            o1 = dx * (pa.y - y1) - dy * (pa.x - x1)
            o2 = dx * (pb.y - y1) - dy * (pb.x - x1)
            if o1 == 0 or o2 == 0 or (o1 > 0) == (o2 > 0):
                continue
            ex, ey = pb.x - pa.x, pb.y - pa.y
            o3 = ex * (y1 - pa.y) - ey * (x1 - pa.x)
            o4 = ex * (y2 - pa.y) - ey * (x2 - pa.x)
            if o3 == 0 or o4 == 0 or (o3 > 0) == (o4 > 0):
                continue
            return (pa.name, pb.name) if pa.name <= pb.name else (pb.name, pa.name)
    return None


def _assign(ctx, jobs, used_bins, binsize) -> dict:
    """Min-cost flow: pads -> via columns (-> pad-pair cuts) -> sink. Returns {job key: option}."""
    S, T = 0, 1
    n = 2 + len(jobs)
    bins, cuts = {}, {}
    edges = [(S, 2 + i, 1, 0.0) for i in range(len(jobs))]
    pick = [None] * len(edges)
    tail = []                                               # (bin node, cut key | None), once each
    seen = set()
    for i, j in enumerate(jobs):
        best = {}
        for o in j.options:
            b = (math.floor(o["via"][0] / binsize), math.floor(o["via"][1] / binsize))
            if b in used_bins:
                continue
            if b not in best or o["cost"] < best[b]["cost"] - 1e-12:
                best[b] = o
        for b in sorted(best):
            o = best[b]
            if b not in bins:
                bins[b] = n
                n += 2
                edges.append((bins[b], bins[b] + 1, 1, 0.0))            # one via per column
                pick.append(None)
            edges.append((2 + i, bins[b], 1, o["cost"]))
            pick.append((i, o))
            ck = o.get("cut")
            if (b, ck) not in seen:
                seen.add((b, ck))
                tail.append((b, ck))
    for b, ck in tail:
        if ck is None:
            edges.append((bins[b] + 1, T, 1, 0.0))
        else:
            if ck not in cuts:
                cuts[ck] = n
                n += 1
            edges.append((bins[b] + 1, cuts[ck], 1, 0.0))
        pick.append(None)
    for ck in sorted(cuts):
        edges.append((cuts[ck], T, max(0, int(ck[2])), 0.0))
        pick.append(None)
    _f, _c, per = flow.min_cost_flow(n, edges, S, T, len(jobs))
    out = {}
    for e, f in enumerate(per):
        if f and pick[e] is not None:
            i, o = pick[e]
            out[jobs[i].key] = o
    return out


def _via_lat(geo, via) -> float:
    return via[0] * geo.t[0] + via[1] * geo.t[1]


def _closes(lat, vr, vclr, lane_t, lane_w, lane_clr) -> bool:
    """Does a via in front of the row at lateral position ``lat`` close the outward lane at ``lane_t``?"""
    return abs(lat - lane_t) < vr + max(vclr, lane_clr) + lane_w / 2 - LANE_TOL


def _hard_lanes(ctx, row, geo, jobs, batch) -> list:
    """Outward lanes an outward via must keep open: every connected pad of the row that is not in the batch
    (its ring slot, or its own position while it has no escape)."""
    reg = registry(ctx)
    out = []
    for p in row.pads:
        k = _key(p)
        if k in batch:
            continue
        rec = reg["by_pad"].get(k)
        if rec is not None:
            if rec.get("lane") is not None:
                out.append((rec["lane"]["lane"], rec["lane"]["w"], rec["lane"]["cl"]))
        elif k in jobs:
            out.append((_tco(geo, p), jobs[k].neck, jobs[k].clr))
    return out


def _unplace(ctx, job) -> None:
    reg = registry(ctx)
    rec = reg["by_pad"].pop(job.key, None)
    if rec is not None:
        _drop(ctx, rec)


def _blocking_pieces(ctx, job, o):
    """Piece ids of the dynamic copper that makes option ``o`` illegal; None when static copper does."""
    orc = ctx.oracle
    w = _widths(job)[-1]
    ca = _carg(job)
    ids = set()
    bl = []
    for x1, y1, x2, y2 in o["segs"]:
        bl += orc.blockers(job.layer, x1, y1, x2, y2, w, job.net, 16, ca)
    bl += orc.via_blockers(o["via"][0], o["via"][1], job.via[0], job.via[1], job.net, 16, ca)
    for b in bl:
        if b.rule in ("thermal", "reserve"):
            continue
        if b.static or b.piece is None:
            return None
        ids.add(b.piece)
    return ids


def _temp_pieces(ctx, job, o, seq) -> list:
    """Oracle-only stand-ins (negative ids, never in the state) for the copper of an option."""
    out = []
    for x1, y1, x2, y2 in o["segs"]:
        if x1 == x2 and y1 == y2:
            continue
        seq[0] -= 1
        out.append(Piece(id=seq[0], net=job.net, conn=-1, kind="seg", layer=job.layer, x1=x1, y1=y1, x2=x2, y2=y2,
                         w=q(o["w"]), stage="fanout"))
    seq[0] -= 1
    out.append(Piece(id=seq[0], net=job.net, conn=-1, kind="via", layer=-1, x1=o["via"][0], y1=o["via"][1],
                     x2=o["via"][0], y2=o["via"][1], w=q(job.via[0]), drill=q(job.via[1]), stage="fanout"))
    for p in out:
        ctx.oracle.add_piece(p)
    return out


def _swap(ctx, job, placed, out) -> bool:
    """A pad left without a via site although it had candidates: move up to two of the escapes that block one
    of them to another of their own candidates (one augmenting step, judged by the oracle on stand-in
    pieces before anything is touched)."""
    reg = registry(ctx)
    owner = {}
    for k in sorted(placed):
        rec = reg["by_pad"].get(k)
        if rec is not None:
            for pid in rec["pieces"]:
                owner[pid] = placed[k][0]
    for o in sorted(job.all_options, key=lambda o: (o["cost"], o["seq"])):
        ids = _blocking_pieces(ctx, job, o)
        if not ids or any(pid not in owner for pid in ids):
            continue
        movers = []
        for pid in sorted(ids):
            if owner[pid] not in movers:
                movers.append(owner[pid])
        if len(movers) > 2:
            continue
        ign = frozenset(pid for m in movers for pid in reg["by_pad"][m.key]["pieces"])
        if _legal_width(ctx, job, o, ign) is not None:
            continue
        seq = [-1_000_000]
        temps = _temp_pieces(ctx, job, o, seq)
        alts = []
        try:
            for m in movers:
                cur = placed[m.key][1]
                alt = None
                for a in sorted(m.all_options, key=lambda a: (a["cost"], a["seq"])):
                    if a is not cur and _legal_width(ctx, m, a, ign) is None:
                        alt = a
                        break
                if alt is None:
                    break
                alts.append((m, alt))
                temps += _temp_pieces(ctx, m, alt, seq)
        finally:
            for p in temps:
                ctx.oracle.remove_piece(p)
        if len(alts) != len(movers):
            continue
        for m in movers:
            _unplace(ctx, m)
        out[job.key] = _place(ctx, job, o)
        placed[job.key] = (job, o)
        for m, alt in alts:
            out[m.key] = _place(ctx, m, alt)
            placed[m.key] = (m, alt)
        return True
    return False


def via_escape(ctx: Ctx, ref: str, pads: list) -> list[dict]:
    """Escape ``pads`` of footprint ``ref`` to through vias: a stub on the pad's layer to a via in front of or
    behind the pad row (grid pads: a dog-bone to an interstitial site). The pads are matched to via columns by
    min-cost flow; every stub and via is oracle-checked against the escapes placed before it; a pad the
    greedy realisation leaves stranded gets one augmenting swap with its neighbours.

    An outward via never closes the exit of a pad that has no via: not of the row's other pads (their lanes
    are kept), and when a pad of the batch itself ends without a via, the batch vias in front of its exit are
    taken back (those pads fail with "via would close a neighbour's exit": give them a ring escape).

    Returns one dict per pad, in input order (same shape as ``ring_fan``, 'kind': 'via',
    'desc': {dir: out | in | dogbone | d<i>, depth, shift}); the escape terminal sits on the via, on every layer."""
    board = ctx.board
    pads = _as_pads(board, ref, pads)
    jobs = _jobs(ctx)
    opt, _explicit = _opt(ctx, ref)
    reg = registry(ctx)
    batch = {_key(p) for p in pads}
    out = {}
    todo = []
    geos = {}
    binsize = None
    for p in pads:
        k = _key(p)
        j = jobs.get(k)
        if j is None:
            out[k] = {"pad": p.name, "ok": False, "skip": "no connection to route from this pad"}
            continue
        if k in reg["by_pad"]:
            out[k] = {"pad": p.name, "ok": False, "skip": "pad already has an escape"}
            continue
        if j.has_copper:
            out[k] = {"pad": p.name, "ok": False, "skip": "pad already has copper"}
            continue
        if j.layer not in p.layers or len(p.layers) > 1 and p.kind not in SMD_KINDS:
            out[k] = {"pad": p.name, "ok": False, "skip": "through-hole pad: reaches every layer already"}
            continue
        row = _row_of(board, p)
        geo = None
        hard = ()
        if row is not None and not row.grid:
            if row.idx not in geos:
                g = _row_geo(ctx, row, jobs, opt)
                geos[row.idx] = (g, _hard_lanes(ctx, row, g, jobs, batch))
            geo, hard = geos[row.idx]
        first = None
        j.options = []
        vr = j.via[0] / 2
        for o in _via_options(ctx, j, row, geo, opt):
            if hard and o["desc"]["dir"] == "out":
                lat = _via_lat(geo, o["via"])
                if any(_closes(lat, vr, j.clr, lt, lw, lc) for lt, lw, lc in hard):
                    first = first or "it would close a neighbour's exit"
                    continue
            r = _legal_width(ctx, j, o)
            if r is None:
                o["cost"] += THERMAL_PRICE * (j.width + j.clr) * o["soft"]
                ck = _crossed_cut(ctx, j, o)
                if ck is not None:
                    cut = _local_cut(ctx, ck[0], ck[1], j.layer, o["w"], j.clr)
                    tracks = int(round(cut.capacity_mm / (o["w"] + j.clr))) if o["w"] + j.clr > 0 else 0
                    if tracks <= 0:
                        first = first or f"no room between {ck[0]} and {ck[1]}"
                        continue
                    o["cut"] = (ck[0], ck[1], tracks)
                j.options.append(o)
            elif first is None:
                first = r
        j.all_options = list(j.options)
        j.blocker = first
        bs = (j.via[0] + j.clr) * _S2
        binsize = bs if binsize is None else min(binsize, bs)
        todo.append(j)
    used_bins = set()
    placed = {}                                             # key -> (job, option) of this batch
    rounds = 0
    max_rounds = 1 + sum(len(j.options) for j in todo)
    while todo and rounds < max_rounds:
        rounds += 1
        cand = [j for j in todo if j.options]
        if not cand:
            break
        choice = _assign(ctx, cand, used_bins, binsize)
        dropped = False
        for j in cand:
            o = choice.get(j.key)
            if o is None:
                continue
            r = _legal_width(ctx, j, o)             # again: the escapes placed in this round are in the oracle now
            if r is None:
                out[j.key] = _place(ctx, j, o)
                placed[j.key] = (j, o)
                used_bins.add((math.floor(o["via"][0] / binsize), math.floor(o["via"][1] / binsize)))
                todo.remove(j)
            else:
                j.options.remove(o)
                j.blocker = j.blocker or r
                dropped = True
        if not dropped:
            break
    # stranded pads: one augmenting swap each
    for j in list(todo):
        if j.all_options and _swap(ctx, j, placed, out):
            todo.remove(j)
    for j in todo:
        reason = "no legal via site" if not j.all_options else "no free via site"
        out[j.key] = _fail(j, reason, j.blocker)
    # a pad of the batch without a via needs its exit: take back the batch vias standing in front of it
    need = list(todo)
    while need:
        j = need.pop(0)
        row = _row_of(board, j.pad)
        if row is None or row.grid or row.idx not in geos:
            continue
        geo = geos[row.idx][0]
        tj = _tco(geo, j.pad)
        for k2 in sorted(placed):
            j2, o2 = placed[k2]
            if o2["desc"]["dir"] != "out" or _row_of(board, j2.pad) is not row:
                continue
            if _closes(_via_lat(geo, o2["via"]), j2.via[0] / 2, j2.clr, tj, j.neck, j.clr):
                _unplace(ctx, j2)
                del placed[k2]
                out[k2] = _fail(j2, "via would close a neighbour's exit")
                need.append(j2)
    return [out[_key(p)] for p in pads]


# ── boxed in ──────────────────────────────────────────────────────────────
def _boxed(ctx, job) -> tuple:
    """Strict test: is every exit of the pad closed by what is on the board for good? Rays at the neck width
    from points along the pad's long axis, 8 directions, to just beyond the pad's copper, judged against the
    static board only (copper of this run can still be ripped: a pad walled in by other escapes or dog-bones
    is left to the detail and repair stages). -> (boxed, blockers of the 4 axial rays from the centre)."""
    orc = ctx.oracle
    dyn = frozenset(p.id for p in ctx.state.pieces())
    pad = job.pad
    w = job.neck
    ca = _carg(job)
    ux, uy = 1.0, 0.0
    for s in pad.shapes.get(job.layer, ()):
        if s.kind == "box":
            ux, uy = s.g[2], s.g[3]
            break
    half = _support(pad, (ux, uy), job.layer)
    hv = _support(pad, (-uy, ux), job.layer)
    if hv > half:
        ux, uy, half = -uy, ux, hv
    reach = max(0.0, half - w / 2)
    starts = [(pad.x + ux * f * reach, pad.y + uy * f * reach) for f in (0.0, 1.0, -1.0, 0.5, -0.5)]
    dirs = []
    for i, (dx, dy) in enumerate(((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))):
        k = 1.0 if i < 4 else _S2
        dirs.append(((dx * ux - dy * uy) * k, (dx * uy + dy * ux) * k))
    layers = [l for l in pad.layers if pad.shapes.get(l)]

    def ray(sx, sy, d, l):
        out_len = _support(pad, d, l) - ((sx - pad.x) * d[0] + (sy - pad.y) * d[1])
        ln = max(out_len, 0.0) + job.clr + w + 0.05
        return q(sx), q(sy), q(sx + d[0] * ln), q(sy + d[1] * ln)

    blockers = []
    free = False
    for d in dirs[:4]:
        for l in layers:
            b = orc.seg_ok(l, *ray(starts[0][0], starts[0][1], d, l), w, job.net, ca, dyn)
            if b is None:
                free = True
            else:
                blockers.append(b)
    if free:
        return False, blockers
    for si, (sx, sy) in enumerate(starts):
        for di, d in enumerate(dirs):
            if si == 0 and di < 4:
                continue
            for l in layers:
                if orc.seg_legal(l, *ray(sx, sy, d, l), w, job.net, ca, dyn):
                    return False, blockers
    return True, blockers


def _boxed_fail(ctx, job, blockers) -> dict:
    """The certificate of a boxed-in pad. Its Cut: on a pad row, the gap between the pad's two neighbours the
    stub has to leave through (design §4.6 pad-pair cut) when that gap carries no track of this width; else
    the pad against whatever closes it (capacity 0)."""
    board = ctx.board
    pad = job.pad
    row = _row_of(board, pad)
    names, nets = [], [job.net]
    for b in sorted(blockers, key=lambda b: (b.dist - b.need, b.tag or "")):
        if b.tag and b.tag not in names:
            names.append(b.tag)
        if b.net and b.net not in nets:
            nets.append(b.net)
    cut = None
    if row is not None and not row.grid:
        i = row.pads.index(pad)
        if 0 < i < len(row.pads) - 1:
            c = _local_cut(ctx, row.pads[i - 1].name, row.pads[i + 1].name, job.layer, job.neck, job.clr, [job.net])
            if c.capacity_mm < c.demand_mm - 1e-9:
                cut = c
    if cut is None:
        cut = Cut(a=pad.name, b=names[0] if names else "edge", layer=board.layers[job.layer], capacity_mm=0.0,
                  demand_mm=round(job.neck + job.clr, 4), nets=nets)
    static = sum(1 for b in blockers if b.static) / len(blockers) if blockers else 1.0
    return {"reason": "pad boxed in", "where": [round(pad.x, 4), round(pad.y, 4)], "blocked_by": nets[1:7],
            "blockers": names[:6], "static_share": round(static, 3), "tried": ["fanout"], "cut": cut,
            "pad": pad.name, "net": job.net}


# ── re-escape (repair rescue) ─────────────────────────────────────────────
def _avoided(o, pad, avoid) -> bool:
    if not avoid:
        return False
    d = o["desc"]
    if avoid in ("out", "in"):
        return (d.get("dir") == "in") == (avoid == "in") if o["kind"] == "via" else avoid == "out"
    if avoid in ("left", "right"):                         # -t / +t of the row
        s = d.get("dir") if o["kind"] == "ring" else d.get("shift")
        return s == (-1 if avoid == "left" else 1)
    v = SIDE_VEC.get(avoid)
    if v is None:
        return False
    ex, ey = o["end"][0] - pad.x, o["end"][1] - pad.y
    ln = math.hypot(ex, ey)
    return ln > 0 and (ex * v[0] + ey * v[1]) > 0.38 * ln


def re_escape(ctx: Ctx, conn: ConnV2, end: str, avoid_side: str) -> bool:
    """Give the pad at ``conn.<end>`` ('a' | 'b') another escape (repair rescue: "alternate fan-out side").

    ``avoid_side``: 'n' | 'e' | 's' | 'w' (an escape that does not head that way), 'left' | 'right' (the row
    direction of a leg, -t / +t), 'out' | 'in' (ring or outward via / inward via), or '' (anything but the
    current one). The side is a preference: when no legal escape avoids it, any escape other than the current
    one is taken. The old stub is replaced in one state transaction. False (nothing changed) when no other
    legal escape exists or another connection is already routed onto the current stub. The connection itself
    must not be routed on the old stub (rip it first)."""
    if end not in ("a", "b"):
        raise ValueError("end must be 'a' or 'b'")
    board = ctx.board
    term = getattr(conn, end)
    if term.pad is None:
        return False
    rec = escape_of(ctx, term.pad, term.x, term.y) if term.escape else None
    if rec is not None and (abs(rec["terminal"].x - term.x) > 1e-6 or abs(rec["terminal"].y - term.y) > 1e-6):
        rec = None
    conns = _conn_by_idx(ctx)
    if rec is not None:
        pad = _find_pad(board, rec["key"][0], rec["key"][1], rec["key"][2])
        users = [(conns[ci], e) for (ci, e) in sorted(rec["orig"]) if ci in conns]
        for c, _e in users:
            if c is not conn and _status(ctx, c) == "routed":
                return False
    elif term.escape:
        return False                                        # an escape terminal this module does not know
    else:
        pad = _find_pad(board, term.pad, term.x, term.y)
        users = [(conn, end)]
    if pad is None or not pad.net or len(pad.layers) != 1 and pad.kind not in SMD_KINDS:
        return False
    # the job, rebuilt from the connections that end on this pad
    jobs = _jobs(ctx)
    j = _Job()
    net = board.net_by_id[pad.net]
    j.pad, j.key, j.net = pad, _key(pad), net.name
    j.conns = users
    j.width = conn.width or net.width
    j.neck = min(conn.neck or net.width, j.width)
    j.cl = conn.clearance or 0.0
    j.clr = max(ctx.oracle.clearance(net.name), j.cl)
    j.via = tuple(conn.via) if conn.via and conn.via[0] > 0 else (net.via, net.drill)
    j.layer = pad.layers[0]
    _layer_width(ctx, j)
    other = conn.b if end == "a" else conn.a
    j.partner = (other.x, other.y)
    j.offlayer, j.near, j.has_copper, j.options, j.result, j.blocker = False, 0.0, False, [], None, None
    j.all_options = []
    jobs = dict(jobs)
    pj = jobs.pop(j.key, None)
    if rec is None and pj is not None:                      # every connection still to route from this pad moves
        j.conns = list(pj.conns)
        if not any(c is conn for c, _e in j.conns):
            j.conns.append((conn, end))
    opt, _explicit = _opt(ctx, pad.ref)
    row = _row_of(board, pad)
    ignore = frozenset(rec["pieces"]) if rec is not None else frozenset()
    cur = (rec["kind"], tuple(sorted(rec["desc"].items()))) if rec is not None else None
    chosen = None
    geo = None
    lanes = cands = probe = None
    if row is not None and not row.grid:
        jobs_geo = dict(jobs)
        if rec is None:
            jobs_geo[j.key] = j
        geo = _row_geo(ctx, row, jobs_geo, opt)
        lanes = _row_lanes(ctx, row, geo, jobs, j.key)
        d0 = _lean(j, geo, opt) or 1
        cands = [(1, 1, 0)] + [(a, b, d) for d in (d0, -d0) for a, b in ((1, 2), (1, 3), (2, 3))]
        probe = (geo.n, geo.t, geo.rs)
    vias = None
    # first an escape that does not head ``avoid_side``; without one, any escape but the current
    for avoid in ((avoid_side, "") if avoid_side else ("",)):
        if cands is not None:
            for a, b, d in cands:
                for w in _widths(j):
                    o = _ring_option(j, geo, a, b, d, w)
                    if (o["kind"], tuple(sorted(o["desc"].items()))) == cur or _avoided(o, pad, avoid):
                        break
                    if not _lane_ok(o["lane"], lanes, geo.rs):
                        continue
                    if _check(ctx, j, o, ignore, probe) is None:
                        chosen = o
                        break
                if chosen is not None:
                    break
        if chosen is None and (opt.get("style") or "auto") != "out":
            if vias is None:
                vias = _via_options(ctx, j, row, geo, opt)
            hard = _hard_lanes(ctx, row, geo, jobs, {j.key}) if geo is not None else ()
            for o in sorted(vias, key=lambda o: (o["cost"], o["seq"])):
                if (o["kind"], tuple(sorted(o["desc"].items()))) == cur or _avoided(o, pad, avoid):
                    continue
                if hard and o["desc"]["dir"] == "out" and any(
                        _closes(_via_lat(geo, o["via"]), j.via[0] / 2, j.clr, lt, lw, lc) for lt, lw, lc in hard):
                    continue
                if _legal_width(ctx, j, o, ignore) is None:
                    chosen = o
                    break
        if chosen is not None:
            break
    if chosen is None:
        return False
    _place(ctx, j, chosen, ignore_rec=rec)
    return True


# ── the stage ─────────────────────────────────────────────────────────────
def _ref_class(ctx) -> tuple:
    b = ctx.board
    df = b.rules.get("default") or {}
    return max(df.get("width") or 0.25, b.rules["track_width"]), b.default_clr


def run(ctx: Ctx, budget: Budget) -> StageReport:
    """Fan out every footprint that needs it. Fills ``registry(ctx)``, moves connection terminals to the
    escape ends, marks the connections of boxed-in pads failed (with their Cut). ``StageReport.data``:
    parts {ref: {escaped, ring, via, unescaped, boxed}}, escapes [...], unescaped [...], boxed [...],
    cuts [Cut], critique [...], failed [v1-format entries of the connections failed here]."""
    rep = StageReport(name="fanout")
    used0 = budget.used
    data = {"parts": {}, "escapes": [], "unescaped": [], "boxed": [], "cuts": [], "critique": [], "failed": []}
    rep.data = data
    mode = getattr(ctx.x, "fanout", "auto")
    parts = getattr(ctx.x, "fanout_parts", None) or (mode if isinstance(mode, dict) else {})
    if mode == "none" and not parts:
        rep.notes.append("fanout: off (plan.global.fanout = 'none')")
        return _finish(ctx, rep, budget, used0)
    jobs = _jobs(ctx)
    registry(ctx)["_jobs"] = jobs                           # one table for the whole stage
    hook_state(ctx)
    try:
        _run(ctx, budget, rep, data, jobs)
    except BaseException:
        registry(ctx).pop("_jobs", None)
        undo(ctx)                                           # never leave escape terminals without their stubs
        raise
    finally:
        registry(ctx).pop("_jobs", None)
    return _finish(ctx, rep, budget, used0)


def _run(ctx, budget, rep, data, jobs) -> None:
    board, orc = ctx.board, ctx.oracle
    refw, refcl = _ref_class(ctx)
    refs = sorted({j.pad.ref for j in jobs.values()})
    charged = orc.checks

    def charge(npads):
        nonlocal charged
        budget.charge(CHARGE_PATTERN * npads + (orc.checks - charged) // ORACLE_CHECKS_PER_UNIT)
        charged = orc.checks

    out_of_budget = False
    for ref in refs:
        opt, explicit = _opt(ctx, ref)
        style = opt["style"]
        if style == "none":
            continue
        if not explicit and not needs_fanout(board, ref, refw, refcl):
            continue
        if budget.spent_out():
            out_of_budget = True
            break
        rows = footprint_rows(board, ref)
        stat = {"escaped": 0, "ring": 0, "via": 0, "unescaped": 0, "boxed": 0}
        in_rows = set()
        results = []                                        # (job, result)
        for row in rows:
            for p in row.pads:
                in_rows.add(id(p))
            if not row.smd and not explicit:
                continue                                    # through-hole rows reach every layer from every side
            rjobs = [jobs[_key(p)] for p in row.pads if _key(p) in jobs]
            if not rjobs:
                continue
            if budget.spent_out():
                out_of_budget = True
                break
            pads = [j.pad for j in rjobs]
            if row.grid or style in ("via", "in") or not row.smd:
                res = via_escape(ctx, ref, pads) if row.smd else ring_fan(ctx, ref, pads, "out")
                charge(len(pads))
            else:
                # auto / out: pads that must change layer try a via first (an outward via never closes a
                # neighbour's exit); everybody else, and whoever got no via, takes the ring; whoever the ring
                # cannot serve tries a via last
                want_layer = opt.get("layer")
                first = [j.pad for j in rjobs if style == "auto" and
                         (j.offlayer or (want_layer is not None and want_layer != j.layer))]
                by = {}
                if first:
                    for p, r in zip(first, via_escape(ctx, ref, first)):
                        by[_key(p)] = r
                    charge(len(first))
                ring_pads = [p for p in pads if not by.get(_key(p), {}).get("ok")]
                tried = set(by)
                if ring_pads and not budget.spent_out():
                    for p, r in zip(ring_pads, ring_fan(ctx, ref, ring_pads, style)):
                        if r.get("ok") or _key(p) not in by:
                            by[_key(p)] = r
                    charge(len(ring_pads))
                if style == "auto" and not budget.spent_out():
                    last = [p for p in ring_pads if "fail" in by.get(_key(p), {}) and _key(p) not in tried]
                    if last:
                        for p, r in zip(last, via_escape(ctx, ref, last)):
                            if r.get("ok"):
                                by[_key(p)] = r
                        charge(len(last))
                res = [by[_key(p)] for p in pads if _key(p) in by]
                pads = [p for p in pads if _key(p) in by]
            results += [(jobs[_key(p)], r) for p, r in zip(pads, res)]
        if explicit and style in ("via", "auto", "in"):
            # pads of the part that sit on no row (a lone fine-pitch pad): dog-bone candidates around the pad
            lone = [j.pad for k, j in sorted(jobs.items()) if j.pad.ref == ref and id(j.pad) not in in_rows
                    and j.pad.kind in SMD_KINDS and j.offlayer]
            if lone:
                results += [(jobs[_key(p)], r) for p, r in zip(lone, via_escape(ctx, ref, lone))]
                charge(len(lone))
        grid_fail = 0
        for j, r in results:
            if r.get("ok"):
                stat["escaped"] += 1
                stat[r["kind"]] += 1
                data["escapes"].append({"pad": r["pad"], "net": j.net, "kind": r["kind"], "desc": r["desc"],
                                        "end": [r["terminal"].x, r["terminal"].y], "width": r["width"],
                                        "necked": r["width"] < j.width - 1e-9})
            elif "fail" in r:
                boxed, blockers = _boxed(ctx, j)
                charge(0)
                if boxed:
                    fail = _boxed_fail(ctx, j, blockers)
                    registry(ctx)["boxed"][j.key] = fail
                    stat["boxed"] += 1
                    data["boxed"].append(dict(fail))
                    data["cuts"].append(fail["cut"])
                    for c, e in j.conns:
                        if _status(ctx, c) == "todo":
                            f = dict(fail)
                            f["probe"] = {"enclosed": True, "end": e}
                            try:
                                ctx.state.set_status(c.idx, "failed", f)
                            except Exception:  # noqa: BLE001
                                continue
                            data["failed"].append({"net": c.net, "conn": c.idx, "from": c.a.pad, "to": c.b.pad,
                                                   "reason": f"pad boxed in ({j.pad.name})", "where": f["where"],
                                                   "blocked_by": f["blocked_by"], "cut": f["cut"],
                                                   "static_share": f["static_share"], "tried": ["fanout"],
                                                   "probe": f["probe"]})
                else:
                    stat["unescaped"] += 1
                    data["unescaped"].append({"pad": r["pad"], "net": j.net, **r["fail"]})
                row = _row_of(board, j.pad)
                if row is not None and row.grid:
                    grid_fail += 1
        if grid_fail:
            data["critique"].append({"kind": "bga_escape", "ref": ref, "value": grid_fail, "limit": 0,
                                     "where": None, "net": None,
                                     "note": f"{grid_fail} pad(s) of {ref} have no legal dog-bone via site: a "
                                             f"BGA this dense needs a smaller via or a hand-made escape"})
        if any(stat.values()):
            data["parts"][ref] = stat
        if out_of_budget:
            break
    if out_of_budget:
        rep.notes.append("fanout: budget spent before every part was fanned out; the rest is left to the detail stage")
    tot = {k: sum(s[k] for s in data["parts"].values()) for k in ("escaped", "ring", "via", "unescaped", "boxed")}
    rep.routed = tot["escaped"]
    rep.failed = tot["boxed"]
    if data["parts"]:
        rep.notes.append(f"fanout: {tot['escaped']} escape(s) on {len(data['parts'])} part(s) ({tot['ring']} ring, "
                         f"{tot['via']} via); {tot['unescaped']} pad(s) left to the detail stage; "
                         f"{tot['boxed']} boxed in")
    for b in data["boxed"]:
        c = b["cut"]
        rep.notes.append(f"{b['pad']} ({b['net']}) is boxed in: cut {c.a} | {c.b} on {c.layer} carries "
                         f"{c.capacity_mm} mm, needs {c.demand_mm} mm")


def _finish(ctx, rep, budget, used0) -> StageReport:
    rep.work = budget.used - used0
    try:
        rep.score = ctx.state.score()
    except Exception:  # noqa: BLE001  (a state without a score yet)
        pass
    return rep
