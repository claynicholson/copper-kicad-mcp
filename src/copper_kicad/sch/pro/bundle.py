"""pro2: the nets two parts of ONE block share, drawn as a planned bundle of parallel wires (class L, "label-only
links inside one block": an MCU and its flash, a programming / SPI header at its IC).

`dock.dock_rows` knows one form only: straight wires, row on row. A peer whose pin ORDER differs from the host's (an
ISP header: MISO, SCK, MOSI against MOSI, MISO, SCK) or whose bus pins sit on BOTH sides (a serial flash) could
therefore only be named, and the router is forbidden to repair that afterwards (it routes one net at a time: no
ordered bundle, so its doglegs and crossings were ruled out one by one). Here the whole bundle is planned at once:

      host                    channel               peer
      pin ── NAME ─────────────┐              ┌────────────────────┐    a pin on the FAR side of the peer is
      pin ── NAME ──────────┐  └──────────────┤ near pin           │    led around the peer's nearer end
      pin ── NAME ─────────┐└─────────────────┤ near pin   far pin ├─┐  (lanes nested) and enters the channel
                           │                  └────────────────────┘ │  on a wrap row, like any other net
                           └─────────────────────────────────────────┘

  * every net runs  host end -> along its row -> ONE vertical track -> along the row of its peer pin (or its wrap
    row) -> peer; a net whose two rows agree is one straight wire and takes no track
  * tracks stand at 2.54 mm beside the peer; the room between the host's wire ends and the first track is as long
    as the longest name, so the names stand in one column at the host's pins (`Cell.name_at`)
  * which track a net takes is a small ordering problem (two-sided channel routing): a net whose host row is
    another net's peer row must turn nearer to the host (else the two would share a piece of that row); among the
    orders that allow the drawing the one with the fewest crossings is taken. Every crossing is square, 2.54 mm or
    more from any wire end. The count has a floor that no drawing can go below - the number of pin pairs whose
    order differs between the two parts - and a bundle that needs more than `max_cross(n)` stays a pair of name
    columns, complete (wired completely or named completely, as before)
  * the peer's pose, its vertical offset and which far pins go over / under it are searched; the peer is slid out
    to the first depth at which it and every wire are free (a bundle wire may cross a wire that is already there -
    the lead of the peer's own supply pin - squarely, counted like any other crossing)

Nothing here can change the netlist: wires are emitted by net key between open ends of that net, checked against
everything the block holds, and the block still passes `route.verify` and the sheet `verify_partition`.
"""
from __future__ import annotations

import itertools
import math
import os
import sys

from ..model import text_width
from .build import U, hit, power_box, power_boxes, snap, wbox

ENABLED = True
P = 2 * U                 # pitch of tracks, lanes and wrap rows
MAX_NETS = 10             # a wider bundle between two parts of a block stays two name columns
MAX_FAR = 4               # far-side pins led around ONE end of the peer
PERM_MAX = 6              # exhaustive track order up to this many turning nets (else four sorted orders)
DEPTH = 64                # grid steps (1.27 mm) the peer may slide out
TRIES = 16                # arrangements (offset, over / under, track order) that are drawn out and checked
CLR = 2.0                 # air around the peer
CROSS_PER_NET = 1.0       # see max_cross
COMB = True               # far pins: the host rows run on past the peer and turn once into their pins (see _find)
COMB_ROWS = 3             # rows of air tried between the peer and the first of those rows
TUCK = True             # a supply / ground lead of the peer on the bundle's rows is led over the tracks (tuck_leads)
STATS = {"planned": 0, "drawn": 0}
DEBUG = bool(os.environ.get("COPPER_BUNDLE_DEBUG"))


def _dbg(*a):
    if DEBUG:
        print("[bundle]", *a, file=sys.stderr)


def max_cross(n: int) -> int:
    """Crossings a bundle of n nets may contain: fewer than one per wire (CROSS_PER_NET = 0.5: half of that). The
    floor is set by the pin orders of the two parts; a bundle that would need more reads worse than two name
    columns."""
    return max(1, int(CROSS_PER_NET * n) - (1 if CROSS_PER_NET >= 1.0 else 0))


_ROOM: dict = {}


def name_room(D, net) -> float:
    if not D.named(net):
        return P
    t = D.text(net)
    r = _ROOM.get(t)
    if r is None:
        if len(_ROOM) > 5000:
            _ROOM.clear()
        r = _ROOM[t] = P * math.ceil((text_width(t) + 3.0) / P)
    return r


# ───────────────────────── track order (no geometry) ─────────────────────────
def _order_cost(order, rows, straight):
    """order = turning nets, nearest the peer first; rows[n] = (host row, peer / wrap row); straight = rows of the
    nets that run straight through. -> crossings among the bundle's own wires, or None when two nets would share a
    piece of a row or a wire would end on another."""
    t = {n: i for i, n in enumerate(order)}
    cross = 0
    for a in order:
        lo, hi = sorted(rows[a])
        for y in straight:
            if lo < y < hi:
                cross += 1
            elif y in (lo, hi):
                return None
        for b in order:
            if b == a:
                continue
            yh, ye = rows[b]
            if t[a] > t[b]:                        # a turns nearer to the host: it meets b's host-side piece
                if lo < yh < hi:
                    cross += 1
                elif yh in (lo, hi):
                    return None
            else:                                  # ... b's peer-side piece
                if lo < ye < hi:
                    cross += 1
                elif ye in (lo, hi):
                    return None
    return cross


_ORDER_MEMO: dict = {}


def floor_crossings(rows: dict) -> int:
    """Pairs of nets whose order on the host differs from their order at the peer: no drawing has fewer crossings."""
    v = list(rows.values())
    return sum(1 for i in range(len(v)) for j in range(i + 1, len(v)) if (v[i][0] - v[j][0]) * (v[i][1] - v[j][1]) < 0)


def best_order(rows: dict, limit: int = 99):
    """-> (crossings, order of the turning nets nearest the peer first) or None. The sorted orders are tried first;
    the search stops at the floor (`floor_crossings`)."""
    names = sorted(rows, key=lambda n: (rows[n], str(n)))
    y0 = min(r[0] for r in rows.values())
    key = tuple((round(rows[n][0] - y0, 2), round(rows[n][1] - y0, 2)) for n in names)
    hit_ = _ORDER_MEMO.get(key)
    if hit_ is not None:
        return None if hit_ == "none" else (hit_[0], [names[i] for i in hit_[1]])
    lb = floor_crossings(rows)
    res = None
    if lb <= limit:
        turning = [n for n in names if abs(rows[n][0] - rows[n][1]) > 0.01]
        straight = [rows[n][0] for n in names if n not in turning]
        orders = [sorted(turning, key=lambda n, f=f: f(rows[n])) for f in
                  (lambda r: r[1], lambda r: -r[1], lambda r: r[0], lambda r: -r[0],
                   lambda r: (r[1] > r[0], r[1]), lambda r: (r[1] > r[0], -r[1]),
                   lambda r: (r[1] < r[0], r[1]), lambda r: (r[1] < r[0], -r[1]))]
        if len(turning) <= PERM_MAX:
            orders = itertools.chain(orders, itertools.permutations(turning))
        for o in orders:
            c = _order_cost(o, rows, straight)
            if c is not None and (res is None or c < res[0]):
                res = (c, list(o))
                if c <= lb:
                    break
    if len(_ORDER_MEMO) > 20000:
        _ORDER_MEMO.clear()
    _ORDER_MEMO[key] = "none" if res is None else (res[0], [names.index(n) for n in res[1]])
    return res


def _tracks(order, rows) -> dict:
    """Track index per turning net (0 = beside the peer): nets whose vertical pieces do not meet share a track."""
    t = {}
    for i, n in enumerate(order):
        lo, hi = sorted(rows[n])
        k = 0
        for e in order[:i]:
            l2, h2 = sorted(rows[e])
            if lo <= h2 + 0.01 and l2 <= hi + 0.01:
                k = max(k, t[e] + 1)
        t[n] = k
    return t


# ───────────────────────── geometry ─────────────────────────
def _segs(poly):
    return [(poly[i], poly[i + 1]) for i in range(len(poly) - 1) if poly[i] != poly[i + 1]]


def _own_crossings(polys: dict):
    """Crossings between the wires of the bundle (exact); None when two of them touch, share a piece of a row or
    cross right beside a bend."""
    items = [(n, a, b) for n, p in polys.items() for a, b in _segs(p)]
    cross = 0
    for i in range(len(items)):
        n1, a1, b1 = items[i]
        for j in range(i + 1, len(items)):
            n2, a2, b2 = items[j]
            if n1 == n2:
                continue
            h1, h2 = abs(a1[1] - b1[1]) < 0.01, abs(a2[1] - b2[1]) < 0.01
            if h1 == h2:
                k = 1 if h1 else 0                 # the axis across the two pieces
                if abs(a1[k] - a2[k]) < P - 0.01 and min(a1[1 - k], b1[1 - k]) < max(a2[1 - k], b2[1 - k]) + 0.01 \
                        and min(a2[1 - k], b2[1 - k]) < max(a1[1 - k], b1[1 - k]) + 0.01:
                    return None                    # closer than a pitch, side by side
                continue
            (ha, hb), (va, vb) = ((a1, b1), (a2, b2)) if h1 else ((a2, b2), (a1, b1))
            x, y = va[0], ha[1]
            x0, x1 = sorted((ha[0], hb[0]))
            y0, y1 = sorted((va[1], vb[1]))
            if x0 - 0.01 <= x <= x1 + 0.01 and y0 - 0.01 <= y <= y1 + 0.01:
                if x0 + P - 0.01 <= x <= x1 - P + 0.01 and y0 + P - 0.01 <= y <= y1 - P + 0.01:
                    cross += 1
                else:
                    return None
    return cross


class _Scene:
    """What a bundle wire may not touch: the boxes of the host block and of the (moved) peer, and the wires among
    them that may be crossed squarely."""

    def __init__(self, B, c, nets):
        self.nets = set(nets)
        self.c = c
        self.host = B.boxes + [b for n, b in B.resv if n not in self.nets]
        self.hkey = {}
        for n, a, b in B.wires:
            self.hkey.setdefault(tuple(round(v, 2) for v in wbox(a, b)), []).append((n, tuple(a), tuple(b)))
        self.hends = [tuple(e) for _n, a, b in B.wires for e in (a, b)] + [tuple(t[1]) for t in B.terms] + \
            [tuple(q) for _n, q, _o, _v in B.powers]
        self.pbox0 = c.boxes + [b for n, b in c.resv if n not in self.nets]
        self.peer, self.wkey, self.ends = [], {}, []
        self.band = {}            # vertical offset -> host boxes level with the peer there
        self.blocked_by_host = False
        self.blocker = None
        # open wire ends of the host that belong to other nets: a bundle that stands in front of them takes the
        # room their own parts and names need (the pins above / below the bus)
        self.other = [(tuple(t[1]), tuple(t[2])) for t in B.terms if t[0] not in self.nets and t[2] is not None]

    def place(self, dx, dy) -> bool:
        """The peer at (dx, dy): False when it is not clear of the host."""
        c = self.c
        self.peer = [[b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy] for b in self.pbox0]
        band = self.band.get(dy)
        if band is None:
            y0, y1 = min(m[1] for m in self.peer) - CLR, max(m[3] for m in self.peer) + CLR
            band = self.band[dy] = [o for o in self.host if o[1] < y1 and y0 < o[3]]
        lo = [min(m[0] for m in self.peer) - CLR, min(m[1] for m in self.peer) - CLR,
              max(m[2] for m in self.peer) + CLR, max(m[3] for m in self.peer) + CLR]
        near = [o for o in band if hit(lo, o, 0)]
        if near and any(hit([m[0] - CLR, m[1] - CLR, m[2] + CLR, m[3] + CLR], o, 0) for m in self.peer for o in near):
            return False
        sh = lambda p: (snap(p[0] + dx), snap(p[1] + dy))             # noqa: E731
        self.wkey = {k: list(v) for k, v in self.hkey.items()}
        for n, a, b in c.wires:
            a, b = sh(a), sh(b)
            self.wkey.setdefault(tuple(round(v, 2) for v in wbox(a, b)), []).append((n, a, b))
        self.ends = self.hends + [sh(e) for _n, a, b in c.wires for e in (a, b)] + [sh(t[1]) for t in c.terms] + \
            [sh(q) for _n, q, _o, _v in c.powers]
        return True

    def seg(self, net, a, b, ta: bool, tb: bool):
        """-> crossings of wires that are already there by the piece a-b of `net` (ta / tb: that end is an existing
        open wire end of the net), or None when the piece is not free."""
        horiz = abs(a[1] - b[1]) < 0.01
        if horiz:
            sg = 1 if b[0] > a[0] else -1
            xa, xb = a[0] + (sg * 0.6 if ta else 0.0), b[0] - (sg * 0.6 if tb else 0.0)
            box = [min(xa, xb), a[1] - 0.3, max(xa, xb), a[1] + 0.3]
        else:
            sg = 1 if b[1] > a[1] else -1
            ya, yb = a[1] + (sg * 0.6 if ta else 0.0), b[1] - (sg * 0.6 if tb else 0.0)
            box = [a[0] - 0.9, min(ya, yb), a[0] + 0.9, max(ya, yb)]
        cross = 0
        nh = len(self.host)
        self.blocked_by_host = False
        for io, o in enumerate(self.host + self.peer):
            if not hit(box, o, 0):
                continue
            ws = self.wkey.get(tuple(round(v, 2) for v in o))
            if not ws:
                self.blocked_by_host = io < nh
                self.blocker = ("host" if io < nh else "peer", [round(v, 2) for v in o])
                return None
            for n2, p, q in ws:
                if n2 == net or (abs(p[1] - q[1]) < 0.01) == horiz:
                    return None                    # its own net (a second wire end), or a wire running along
                if horiz:
                    x, y = p[0], a[1]
                    ok = min(a[0], b[0]) + P - 0.01 <= x <= max(a[0], b[0]) - P + 0.01 and \
                        min(p[1], q[1]) + P - 0.01 <= y <= max(p[1], q[1]) - P + 0.01
                else:
                    x, y = a[0], p[1]
                    ok = min(a[1], b[1]) + P - 0.01 <= y <= max(a[1], b[1]) - P + 0.01 and \
                        min(p[0], q[0]) + P - 0.01 <= x <= max(p[0], q[0]) - P + 0.01
                if not ok or any(max(abs(e[0] - x), abs(e[1] - y)) < P - 0.01 for e in self.ends):
                    return None
                cross += 1
        return cross


class Plan:
    def __init__(self):
        self.dx = self.dy = 0.0
        self.polys = {}           # net -> [points], host end first
        self.cross = 0            # crossings the bundle adds (its own + wires that were there)
        self.bends = 0
        self.length = 0.0
        self.hend = {}            # net -> host end used
        self.pend = {}            # net -> peer end used (moved)
        self.nets = []
        self.covered = 0          # other pin rows of the host that the bundle and the peer stand in front of

    def key(self):
        return (self.cross, self.covered, self.bends, round(self.length, 1))


def _peer_ends(c, nets, s) -> dict:
    """net -> ('near' | 'far', open end of the peer): the end that looks at the host when there is one."""
    ends = {}
    for n, pt, o in c.terms:
        if n not in nets or o[1] != 0:
            continue
        side = "near" if o[0] == -s else "far"
        cur = ends.get(n)
        if cur is None or (cur[0] == "far" and side == "near") or \
                (cur[0] == side and (pt[1], pt[0]) < (cur[1][1], cur[1][0])):
            ends[n] = (side, (pt[0], pt[1]))
    return ends


def clear_rows(c, rows: list, s: int, x_from: float) -> int:
    """A supply / ground symbol of the peer that stands ON one of `rows` in front of the pins (the ground of a
    header's pin 2, drawn beyond the names of the pins below it) is moved along its lead until the rows are free:
    supplies up, grounds down. The lead then crosses the wires of those rows squarely. -> symbols moved."""
    moved = 0
    for i, (n, q, o, v) in enumerate(list(c.powers)):
        if o not in ("up", "down") or (q[0] - x_from) * s > 0.01:
            continue                                   # not in front of the pins
        sg = -1 if o == "up" else 1
        w = next((j for j, (n2, a, b) in enumerate(c.wires)
                  if n2 == n and abs(a[0] - b[0]) < 0.01 and tuple(b) == tuple(q) and (b[1] - a[1]) * sg > 0), None)
        if w is None:
            w = next((j for j, (n2, a, b) in enumerate(c.wires)
                      if n2 == n and abs(a[0] - b[0]) < 0.01 and tuple(a) == tuple(q) and (a[1] - b[1]) * sg > 0), None)
        if w is None:
            continue
        y = q[1]
        for _ in range(12):
            lo, hi = sorted((y, y + sg * 5.9))
            hitr = [r for r in rows if lo - 1.0 < r < hi + 1.0]
            if not hitr:
                break
            y = (min(hitr) - P) if o == "up" else (max(hitr) + P)
        if abs(y - q[1]) < 0.01:
            continue
        n2, a, b = c.wires[w]
        other = a if tuple(b) == tuple(q) else b
        nq = (q[0], snap(y))
        old = [wbox(a, b), power_box(v, q, o)] + power_boxes(v, q, o)
        c.boxes = [bx for bx in c.boxes if not any(all(abs(u - t) < 0.001 for u, t in zip(bx, ob)) for ob in old)]
        c.wires[w] = (n2, other, nq)
        c.powers[i] = (n, nq, o, v)
        c.boxes += [wbox(other, nq)] + power_boxes(v, nq, o)
        moved += 1
    return moved


def find(B, c, hterm: dict, out, skip=(), limit=None):
    """The best drawing of the nets `hterm` ({net: [open wire ends of the host looking along `out`]}) to the open
    ends of the peer cell `c` (posed, anywhere). Nothing is changed. -> Plan or None."""
    D = B.D
    s = out[0]
    if not ENABLED or s == 0 or out[1] != 0:
        return None
    he = {n: [tuple(p) for p in pts if tuple(p) not in skip] for n, pts in hterm.items()}
    he = {n: pts for n, pts in he.items() if pts}
    pe = _peer_ends(c, he, s)
    # every net the two parts could share a wire for is in the bundle - or none is (one way of drawing per pair)
    if set(pe) != set(he) or not (2 <= len(pe) <= MAX_NETS):
        _dbg("no bundle: host nets", sorted(D.text(n) for n in he), "peer ends", sorted(D.text(n) for n in pe))
        return None
    nets = sorted(pe, key=lambda n: (min(p[1] for p in he[n]), n))
    hy0 = he[nets[0]][0][1]
    if any(abs((p[1] - hy0) / P - round((p[1] - hy0) / P)) > 0.01 for n in nets for p in he[n]):
        return None                                # host rows off the 2.54 mm pitch
    py0 = pe[nets[0]][1][1]
    if any(abs((pe[n][1][1] - py0) / P - round((pe[n][1][1] - py0) / P)) > 0.01 for n in nets):
        return None
    if len({round(p[1], 2) for n in nets for p in he[n]}) < sum(len(he[n]) for n in nets):
        return None                                # two host ends on one row
    near = [n for n in nets if pe[n][0] == "near"]
    far = sorted((n for n in nets if pe[n][0] == "far"), key=lambda n: pe[n][1][1])
    if not near:
        return None
    lim = max_cross(len(nets)) if limit is None else limit
    # a supply / ground symbol of the peer that stands on the rows of the bundle is in the way. Two ways out, both
    # planned, the better drawing kept: its lead runs on THROUGH the rows (clear_rows: it crosses their wires), or
    # it is led over the tracks and stands beyond them (tuck_leads: it crosses nothing when the wires turn away)
    rows_ = [pe[n][1][1] for n in near]
    x_from = min(s * pe[n][1][0] for n in near) * s
    st0 = _lead_state(c)
    clear_rows(c, rows_, s, x_from)
    best = _find_memo(B, c, D, s, nets, he, pe, near, far, lim, py0)
    if TUCK:
        st_a = _lead_state(c)
        _lead_restore(c, st0)
        if tuck_leads(c, rows_, s, x_from, len(nets), pe):
            alt = _find_memo(B, c, D, s, nets, he, pe, near, far, lim, py0)
            _dbg("leads over the tracks:", alt.key() if alt else None, "through the rows:", best.key() if best else None)
            if alt is not None and (best is None or alt.key() < best.key()):
                return alt
        _lead_restore(c, st_a)
    return best


def _lead_state(c) -> tuple:
    return (list(c.boxes), list(c.wires), list(c.powers), list(getattr(c, "tucked", ())))


def _lead_restore(c, st: tuple):
    c.boxes, c.wires, c.powers, c.tucked = list(st[0]), list(st[1]), list(st[2]), list(st[3])


def tuck_leads(c, rows: list, s: int, x_from: float, ntr: int, pe: dict) -> int:
    """A supply / ground symbol of the peer that stands ON one of `rows` in front of the pins (the ground of a
    header's pin 2, drawn beyond the names of the signal pins below it) is led OVER the tracks of the bundle: its
    lead runs along its own pin row past the last track and turns to the symbol there. The wires of the rows below
    turn before they reach it - when they turn away from the lead (the host's rows lie below a ground pin) nothing
    is crossed at all.

        2 ─────────────┐         (tracks of rows 3 .. 5 between the pins and the ground)
        3 ──────────┐  ⏚
        4 ────────┐ │
        5 ──────┐ │ │
    -> symbols re-seated (c.tucked = their boxes: the tracks start at the pins, not beyond these symbols)."""
    todo = []
    for i, (n, q, o, v) in enumerate(c.powers):
        if o not in ("up", "down") or (q[0] - x_from) * s > 0.01:
            continue
        sg = -1 if o == "up" else 1
        lo, hi = sorted((q[1], q[1] + sg * 5.9))
        if not any(lo - 1.0 < r < hi + 1.0 for r in rows):
            continue
        w = next((j for j, (n2, a, b) in enumerate(c.wires) if n2 == n and abs(a[0] - b[0]) < 0.01
                  and tuple(q) in (tuple(a), tuple(b))), None)
        corner = tuple(q)
        if w is not None:
            a, b = c.wires[w][1], c.wires[w][2]
            corner = tuple(a) if tuple(b) == tuple(q) else tuple(b)
        h = [j for j, (n2, a, b) in enumerate(c.wires) if n2 == n and abs(a[1] - b[1]) < 0.01
             and corner in (tuple(a), tuple(b))]
        if len(h) != 1 or any(n2 == n and j not in (w, h[0]) and corner in (tuple(a), tuple(b))
                              for j, (n2, a, b) in enumerate(c.wires)):
            continue                                   # not a plain L from one pin
        a, b = c.wires[h[0]][1], c.wires[h[0]][2]
        p0 = tuple(a) if tuple(b) == corner else tuple(b)
        if (corner[0] - p0[0]) * s > -0.01 or any(abs(p0[1] - r) < 0.01 for r in rows):
            continue                                   # (the lead must leave towards the host, on a row of its own)
        todo.append((i, n, tuple(q), o, v, w, h[0], corner, p0))
    if not todo:
        return 0
    gone = []
    for i, n, q, o, v, w, h, corner, p0 in todo:
        gone += [wbox(p0, corner), power_box(v, q, o)] + power_boxes(v, q, o) + ([wbox(corner, q)] if w is not None else [])
    keep = [bx for bx in c.boxes if not any(all(abs(u - t) < 0.001 for u, t in zip(bx, ob)) for ob in gone)]
    rest = keep + [b for n, b in c.resv if n not in pe]
    # the first track stands a pitch before the pin ends, clear of what is left of the peer (as _realise places it)
    qs = min(s * pe[n][1][0] for n in pe if pe[n][0] == "near")
    q0 = s * pe[min(pe, key=lambda n: (pe[n][0] != "near", pe[n][1][1]))][1][0]
    t0 = min(min(s * b_[0 if s > 0 else 2] for b_ in rest) - 1.0, qs - P)
    t0 = q0 - math.ceil((q0 - t0) / P - 1e-6) * P
    # ... and the symbol a pitch beyond the last track, or two when its value text is wider than that
    tw = max(max(2.6, text_width(v) + 0.6) for _i, _n, _q, _o, v, _w, _h, _c, _p in todo)
    xg = snap(s * (t0 - (ntr - 1) * P - P * math.ceil((tw / 2 + 1.0) / P - 1e-6)))
    moved, new = 0, []
    for i, n, q, o, v, w, h, corner, p0 in todo:
        sg = -1 if o == "up" else 1
        lead = abs(q[1] - corner[1])
        nc = (xg, p0[1])
        nq = (xg, snap(p0[1] + sg * lead))
        add = [wbox(p0, nc)] + ([wbox(nc, nq)] if lead > 0.01 else []) + power_boxes(v, nq, o)
        # ... and a row of air beyond the symbol: the wire that passes there carries its name right at this spot
        # (the names of the bundle stand in one column at the host's pins)
        tb = add[-1]
        add.append([tb[0], tb[3], tb[2], tb[3] + P] if sg > 0 else [tb[0], tb[1] - P, tb[2], tb[1]])
        run = wbox((p0[0] - s * 0.6, p0[1]), nc, 0.02)
        if any(hit(bx, o2, 0) for bx in add[1:] + [run] for o2 in rest):
            return 0                                   # no room there: the peer is left as it was drawn
        new.append((i, n, o, v, w, h, p0, nc, nq, add))
    c.boxes = keep
    c.tucked = list(getattr(c, "tucked", ()))
    for i, n, o, v, w, h, p0, nc, nq, add in new:
        c.wires[h] = (n, p0, nc)
        if w is not None:
            c.wires[w] = (n, nc, nq)
        c.powers[i] = (n, nq, o, v)
        c.boxes += add
        c.tucked += add
        moved += 1
    return moved


def _find_memo(B, c, D, s, nets, he, pe, near, far, lim, py0):
    # the engine draws a sheet several times (flags, measured frame sizes): the same block, the same peer, the same
    # question. The answer depends on nothing but the geometry below and the room the names take
    memo = (s, lim, COMB, tuple((n, tuple(he[n]), name_room(D, n)) for n in nets), _print(B), _print(c))
    got = _FIND_MEMO.get(memo)
    if got is not None:
        STATS["memo"] = STATS.get("memo", 0) + 1
        return None if got == "none" else got
    best = _find(B, c, D, s, nets, he, pe, near, far, lim, py0)
    if len(_FIND_MEMO) > 600:
        _FIND_MEMO.clear()
    _FIND_MEMO[memo] = "none" if best is None else best
    return best


_FIND_MEMO: dict = {}


def _print(x) -> int:
    """Fingerprint of everything a cell holds that a bundle can touch."""
    return hash((tuple(tuple(b) for b in x.boxes), tuple((n, tuple(b)) for n, b in x.resv),
                 tuple((n, tuple(a), tuple(b)) for n, a, b in x.wires),
                 tuple((t[0], tuple(t[1]), tuple(t[2]) if t[2] is not None else None) for t in x.terms),
                 tuple((n, tuple(q), o, v) for n, q, o, v in x.powers)))


def _find(B, c, D, s, nets, he, pe, near, far, lim, py0):
    pboxes = c.boxes + [b for n, b in c.resv if n not in pe]
    top = min(b[1] for b in pboxes)
    bot = max(b[3] for b in pboxes)
    tucked = getattr(c, "tucked", ())                                 # (leads that pass over the tracks: tuck_leads)
    nearx = min(s * b[0 if s > 0 else 2] for b in pboxes if not any(b is t for t in tucked)) * s   # the peer's edge towards the host
    farx = max(s * b[2 if s > 0 else 0] for b in pboxes) * s

    def gy(v, up: bool):                                           # onto the peer's row pitch, away from the body
        k = (v - py0) / P
        return py0 + (math.floor(k + 1e-6) if up else math.ceil(k - 1e-6)) * P
    wrap_top = gy(top - 1.0, True)
    wrap_bot = gy(bot + 1.0, False)
    cands = []
    for kf in range(0, len(far) + 1):                               # the first kf far pins go over the peer
        if kf > MAX_FAR or len(far) - kf > MAX_FAR:
            continue
        ye, lane = {}, {}
        for n in near:
            ye[n] = pe[n][1][1]
        for j, n in enumerate(far[:kf]):
            ye[n], lane[n] = wrap_top - j * P, j
        for j, n in enumerate(reversed(far[kf:])):
            ye[n], lane[n] = wrap_bot + j * P, j
        dys = {round(p[1] - ye[n], 2) for n in nets for p in he[n]}
        # ... and every offset in between and a few rows beyond: with the tracks at the host no net has to run straight
        d0 = min(dys)
        dys |= {round(d0 + i * P, 2) for i in range(-3, int(round((max(dys) - d0) / P)) + 4)}
        for dy in sorted(dys):
            rows, hsel = {}, {}
            for n in nets:
                hp = next((p for p in he[n] if abs(p[1] - ye[n] - dy) < 0.01), None) or \
                    min(he[n], key=lambda p, n=n: (abs(p[1] - ye[n] - dy), p[1]))
                hsel[n] = hp
                rows[n] = (round(hp[1], 2), round(ye[n] + dy, 2))
            if floor_crossings(rows) > lim:
                continue
            bo = best_order(rows, lim)
            if bo is None or bo[0] > lim:
                continue
            span = sum(abs(r[0] - r[1]) for r in rows.values())
            # (second term: half the bends of the drawing - a far net turns twice more than a near one, whether it
            # takes a track or not; the comb candidates below are counted the same way)
            cands.append(((bo[0], len(bo[1]) + len(far), span, kf, dy), dy, rows, hsel, bo[1], dict(lane)))
    if COMB and far:
        # the comb: the peer stands wholly above (or below) the host rows of its far pins. Those rows run on under
        # (over) the peer and turn ONCE into their pin beyond it - no wrap row, no track, no hairpin back under the
        # part (the three nested loops around a serial flash). Lanes stay nested by pin order
        seen = {(c_[1], tuple(sorted(c_[2].items())), tuple(sorted(c_[3].items()))) for c_ in cands}
        for under, pick in ((True, min), (False, min), (True, max), (False, max)):
            if pick is max and all(len(he[n]) == 1 for n in far):
                continue
            fh = {n: pick(he[n], key=lambda p: p[1]) for n in far}
            lane = {n: j for j, n in enumerate(reversed(far) if under else far)}
            lim_y = min(p[1] for p in fh.values()) if under else max(p[1] for p in fh.values())
            for k in range(0, COMB_ROWS):
                dy = round((lim_y - wrap_bot - k * P) if under else (lim_y - wrap_top + k * P), 2)
                rows, hsel = {}, {}
                for n in near:
                    hp = min(he[n], key=lambda p, n=n: (abs(p[1] - pe[n][1][1] - dy), p[1]))
                    hsel[n] = hp
                    rows[n] = (round(hp[1], 2), round(pe[n][1][1] + dy, 2))
                for n in far:
                    hsel[n] = fh[n]
                    rows[n] = (round(fh[n][1], 2), round(fh[n][1], 2))
                if floor_crossings(rows) > lim:
                    continue
                bo = best_order(rows, lim)
                if bo is None or bo[0] > lim:
                    continue
                sk = (dy, tuple(sorted(rows.items())), tuple(sorted(hsel.items())))
                if sk in seen:
                    continue                           # (the wrap rows happen to be the host rows: planned above)
                seen.add(sk)
                span = sum(abs(r[0] - r[1]) for r in rows.values())
                cands.append(((bo[0], len(bo[1]) + len(far), span, 0 if under else len(far), dy), dy, rows, hsel, bo[1], dict(lane)))
    cands.sort(key=lambda x: x[0])
    _dbg([D.text(n) for n in nets], "near", len(near), "far", len(far), "limit", lim, "candidates", [x[0] for x in cands[:6]])
    STATS["planned"] += 1
    sc = _Scene(B, c, nets)
    qn = min(s * pe[n][1][0] for n in near)                          # the near pin end closest to the host (along s)
    best = None
    for _key, dy, rows, hsel, order, lane in cands[:TRIES]:
        if best is not None and best.cross <= _key[0]:
            break
        for at_host in (False, True):
            pl = _realise(sc, D, s, nets, pe, hsel, rows, order, lane, dy, nearx, farx, qn, lim, at_host)
            _dbg("  cand", _key, "tracks at host" if at_host else "tracks at peer", "->", pl.key() if pl else None)
            if pl is not None and (best is None or pl.key() < best.key()):
                best = pl
            if pl is not None or not order:
                break
    return best


def _realise(sc, D, s, nets, pe, hsel, rows, order, lane, dy, nearx, farx, qn, lim, at_host=False):
    """The peer slid out along s to the first depth at which it and all wires are free. The tracks stand beside the
    peer, or (at_host) right behind the names at the host's wire ends - there the rows of the PEER carry the long
    pieces, which helps when a host row is blocked further out."""
    trk = _tracks(order, rows)
    edge = max(s * hsel[n][0] for n in nets)                        # outermost host end (along s)
    hx0 = hsel[nets[0]][0]

    def toward(v):                                                  # the host's 2.54 mm column at v or nearer the host
        return snap(hx0 + s * math.floor(s * (v - hx0) / P + 1e-6) * P)

    def away(v):
        return snap(hx0 + s * math.ceil(s * (v - hx0) / P - 1e-6) * P)
    room_t = max([name_room(D, n) for n in order] or [P])
    q0 = pe[nets[0]][1][0]
    base = snap(toward(q0) - q0)                                    # the peer's pin ends on the host's column pitch
    ntr = (max(trk.values()) + 1) if trk else 0
    best = None
    why = {}
    own = -1
    for m in range(-2 * DEPTH, 2 * DEPTH):
        dx = snap(base + s * m * P)
        if s * (nearx + dx) < edge + P - 0.01:
            continue
        if s * (nearx + dx) > edge + DEPTH * U + (ntr + 2) * P + room_t:
            break
        # first track: clear of the peer's edge, a pitch or more before its pin ends
        t0 = toward(s * min(s * (nearx + dx) - 1.0, qn + s * dx - P))
        if at_host:
            th = away(s * (edge + room_t)) + s * (ntr - 1) * P
            if s * th > s * t0 + 0.01:
                continue
            t0 = snap(th)
        tx = {n: snap(t0 - s * trk[n] * P) for n in order}
        if order and min(s * tx[n] for n in order) < edge + room_t - 0.01:
            why["name room"] = why.get("name room", 0) + 1
            continue                                               # no room for the names before the first track
        l0 = away(farx + dx + s * 1.0)
        ok = True
        polys = {}
        for n in nets:
            hp = hsel[n]
            side, q = pe[n]
            q = (snap(q[0] + dx), snap(q[1] + dy))
            yh, ye = rows[n]
            if side == "near":
                if n in trk:
                    if s * (tx[n] - hp[0]) < name_room(D, n) - 0.01 or s * (q[0] - tx[n]) < P - 0.01:
                        ok = False
                        break
                    polys[n] = [hp, (tx[n], yh), (tx[n], ye), q]
                else:
                    if s * (q[0] - hp[0]) < max(P, name_room(D, n)) - 0.01:
                        ok = False
                        break
                    polys[n] = [hp, q]
            else:
                xl = snap(l0 + s * lane[n] * P)
                if s * (xl - q[0]) < P - 0.01:
                    ok = False
                    break
                if n in trk:
                    if s * (tx[n] - hp[0]) < name_room(D, n) - 0.01:
                        ok = False
                        break
                    polys[n] = [hp, (tx[n], yh), (tx[n], ye), (xl, ye), (xl, q[1]), q]
                else:
                    polys[n] = [hp, (xl, yh), (xl, q[1]), q]
        if not ok:
            why["short"] = why.get("short", 0) + 1
            continue
        if own == -1:                                          # (the same at every depth: only lengths change)
            own = _own_crossings(polys)
        if own is None or own > lim:
            why[f"own {own}"] = why.get(f"own {own}", 0) + 1
            break
        if not sc.place(dx, dy):
            why["peer not clear"] = why.get("peer not clear", 0) + 1
            continue
        cross = own
        stuck = False
        for n in nets:
            sg = _segs(polys[n])
            for i, (a, b) in enumerate(sg):
                x = sc.seg(n, a, b, i == 0, i == len(sg) - 1)
                if x is None:
                    kk = f"{D.text(n)} piece {i} blocked by {getattr(sc, 'blocker', None)}"
                    why[kk] = why.get(kk, 0) + 1
                    ok = False
                    if i == 0 and sc.blocked_by_host and len(sg) > 1:
                        stuck = True                               # the host's own row is blocked: further out it still is
                    break
                cross += x
            if not ok or cross > lim:
                ok = False
                break
        if not ok:
            if stuck:
                break
            continue
        pl = Plan()
        pl.dx, pl.dy, pl.polys, pl.cross, pl.nets = dx, dy, polys, cross, list(nets)
        pl.bends = sum(len(_segs(p)) - 1 for p in polys.values())
        pl.length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for p in polys.values() for a, b in _segs(p))
        pl.hend = {n: hsel[n] for n in nets}
        pl.pend = {n: polys[n][-1] for n in nets}
        ys = [q[1] for p_ in polys.values() for q in p_] + [m_[1] for m_ in sc.peer] + [m_[3] for m_ in sc.peer]
        xs = [q[0] for p_ in polys.values() for q in p_] + [m_[0] for m_ in sc.peer] + [m_[2] for m_ in sc.peer]
        pl.covered = sum(1 for q, o in sc.other if o[0] == s and o[1] == 0 and min(ys) - 0.01 <= q[1] <= max(ys) + 0.01
                         and (max(xs) - q[0]) * s > 0 and (min(xs) - q[0]) * s > -DEPTH * U)
        if best is None or pl.key() < best.key():
            best = pl
        if cross == own:
            break                                                  # nothing gained by sliding further out
    if best is None:
        _dbg("    realise failed:", why)
    return best


def commit(B, c, pl: Plan, out) -> None:
    """Draw the plan: the peer moves to its place and becomes part of the block, the wires are added by net key,
    the names of the bundle are asked to stand at the host's ends (one column)."""
    D = B.D
    c.shift(pl.dx, pl.dy)
    ends = set(pl.pend.values())

    def at_end(b):
        return any(b[0] - 0.4 <= p[0] <= b[2] + 0.4 and b[1] - 0.4 <= p[1] <= b[3] + 0.4 for p in ends)
    c.resv = [(n, b) for n, b in c.resv if not (n in pl.polys and at_end(b))]
    ends = set(pl.hend.values())
    B.resv = [(n, b) for n, b in B.resv if not (n in pl.polys and at_end(b))]     # the names move onto the wires
    B.absorb(c)
    for n in pl.nets:
        for a, b in _segs(pl.polys[n]):
            B.wires.append((n, a, b))
            B.boxes.append(wbox(a, b))
        if D.named(n):
            B.name_at.append((n, pl.hend[n], tuple(out)))
        # the first piece of the wire, for the parts of this net that were asked to wait (dock.tap_deferred)
        sg = _segs(pl.polys[n])
        if sg and abs(sg[0][0][1] - sg[0][1][1]) < 0.01:
            B.runs.append((n, sg[0][0], sg[0][1], tuple(out), len(sg) > 1))
    STATS["drawn"] += 1
