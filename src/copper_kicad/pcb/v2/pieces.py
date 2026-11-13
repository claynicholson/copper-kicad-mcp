"""RouteState: transactional copper (design §4.2).

Every segment / via v2 places is a ``Piece`` stored here by id, indexed by connection and by net,
and mirrored into the oracle's dynamic layer (``add`` / ``remove``), so the oracle always judges
against exactly the copper of the state.

    transactions   ``tok = begin()`` ... ``commit(tok)`` | ``rollback(tok)``; they nest. A rollback
                   undoes every add / remove / status change / ``new_errors`` change since ``begin``.
                   Outside a transaction nothing is logged.
    snapshots      ``snapshot()`` is an immutable copy (pieces + status map + new_errors);
                   ``restore()`` diffs it against the current set (and is itself undoable).
    connectivity   ``connected(conn)``: is terminal a joined to terminal b through this net's pieces
                   and the board's static clusters (pads, kept tracks / vias, pour islands)?
    score          ``score()`` -> types.Score; sums are kept in integer nanometres, so the score of a
                   state never depends on the order in which its pieces were added and removed.

Piece ids are monotone and never reused; a piece that comes back (rollback, restore) keeps its id.
Rip granularity is the connection (``rip_conn``): nothing here can remove a whole net.

Connectivity rules (per net; an "end" is a segment end point or a via centre):

    piece - piece    strict: an end of one lies within TEE_TOL of the other's centre line (coincident
                     ends and tees); loose: an end lies inside the other's copper (KiCad's own rule).
    piece - cluster  an end lies inside static copper of the cluster on that layer: a pad shape, a
                     kept track or via, a pour island (the same in strict and loose mode).

``strict`` is what v2's own stages should produce (centre-line joins) and implies KiCad connectivity;
``loose`` is for copper made by v1's search (legacy.py), whose paths may end anywhere inside a wide
pad, a track or a pour.
"""
from __future__ import annotations

import math

from ..guided import geom
from .oracle import Oracle
from .types import ConnV2, Piece, Score, Terminal, q

TEE_TOL = 1.5e-6          # mm: "on the centre line" (the design's 1 nm + the 1 nm quantisation of both items)
STATUSES = ("todo", "routed", "failed")
_NM = 1e6
_BUCKET = 1.0             # mm, cell of the per-net connectivity buckets
_PIECE_FIELDS = ("net", "conn", "kind", "layer", "x1", "y1", "x2", "y2", "w", "drill", "stage", "protected", "target_w")


def is_open(t: Terminal) -> bool:
    """An *open* terminal: "anything else of this net" (v1's pad-to-pour connection). Convention: no pad,
    no cluster and no layers."""
    return t.pad is None and not t.cluster and not t.layers


class Snapshot:
    """Immutable copy of a RouteState: pieces (by id), status map, failure records, new_errors."""
    __slots__ = ("pieces", "status", "fails", "new_errors")

    def __init__(self, pieces, status, fails, new_errors):
        object.__setattr__(self, "pieces", pieces)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "fails", fails)
        object.__setattr__(self, "new_errors", new_errors)

    def __setattr__(self, k, v):
        raise AttributeError("Snapshot is immutable")

    def __eq__(self, o):
        return (isinstance(o, Snapshot) and self.pieces == o.pieces and self.status == o.status
                and self.new_errors == o.new_errors)

    def __hash__(self):
        return hash((self.pieces, self.status, self.new_errors))

    def __repr__(self):
        return f"Snapshot({len(self.pieces)} pieces, {sum(1 for _c, s in self.status if s == 'routed')} routed)"


def _cells(x1, y1, x2, y2, r):
    """Bucket keys a capsule (segment grown by r) can touch."""
    c = _BUCKET
    ix0, ix1 = int(math.floor((min(x1, x2) - r) / c)), int(math.floor((max(x1, x2) + r) / c))
    iy0, iy1 = int(math.floor((min(y1, y2) - r) / c)), int(math.floor((max(y1, y2) + r) / c))
    n = (ix1 - ix0 + 1) * (iy1 - iy0 + 1)
    if n <= 12:
        return [(ix, iy) for iy in range(iy0, iy1 + 1) for ix in range(ix0, ix1 + 1)]
    # a long diagonal: walk it instead of filling its bounding box
    out = set()
    ln = math.hypot(x2 - x1, y2 - y1)
    k = int(ln / (0.5 * c)) + 1
    for i in range(k + 1):
        t = i / k
        x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        jx0, jx1 = int(math.floor((x - r - 0.25 * c) / c)), int(math.floor((x + r + 0.25 * c) / c))
        jy0, jy1 = int(math.floor((y - r - 0.25 * c) / c)), int(math.floor((y + r + 0.25 * c) / c))
        for iy in range(jy0, jy1 + 1):
            for ix in range(jx0, jx1 + 1):
                out.add((ix, iy))
    return sorted(out)


def _touch(a: Piece, b: Piece):
    """None = not joined; False = joined by KiCad's rule only (an end inside the other's copper);
    True = joined on the centre line (within TEE_TOL)."""
    av, bv = a.kind == "via", b.kind == "via"
    if av and bv:
        d = math.hypot(a.x1 - b.x1, a.y1 - b.y1)
        if d <= TEE_TOL:
            return True
        return False if d <= max(a.w, b.w) / 2 else None
    if av or bv:
        v, s = (a, b) if av else (b, a)
        d_line = geom.pt_seg_dist(v.x1, v.y1, s.x1, s.y1, s.x2, s.y2)
        if d_line > max(s.w, v.w) / 2:
            return None
        if d_line <= TEE_TOL:
            return True
        d_end = min(math.hypot(s.x1 - v.x1, s.y1 - v.y1), math.hypot(s.x2 - v.x1, s.y2 - v.y1))
        return False if (d_line <= s.w / 2 or d_end <= v.w / 2) else None
    if a.layer != b.layer:
        return None
    da = min(geom.pt_seg_dist(a.x1, a.y1, b.x1, b.y1, b.x2, b.y2), geom.pt_seg_dist(a.x2, a.y2, b.x1, b.y1, b.x2, b.y2))
    db = min(geom.pt_seg_dist(b.x1, b.y1, a.x1, a.y1, a.x2, a.y2), geom.pt_seg_dist(b.x2, b.y2, a.x1, a.y1, a.x2, a.y2))
    if da <= TEE_TOL or db <= TEE_TOL:
        return True
    return False if (da <= b.w / 2 or db <= a.w / 2) else None


class _NetGraph:
    """Connectivity of one net: its pieces and the board's static clusters.
    Nodes are piece ids (> 0) and ``-cluster id`` (< 0)."""
    __slots__ = ("L", "static", "cids", "pieces", "buckets", "adj", "comp", "version")

    def __init__(self, board, net: str):
        self.L = board.L
        self.pieces = {}
        self.buckets = {}         # cell -> set of piece ids
        self.adj = {}             # node -> {node: strict}
        self.comp = {}            # loose flag -> {node: component index}
        self.version = 0
        # static copper of every cluster, bucketed
        st = self.static = {}     # cell -> [(cid, kind, data)]
        cl = board.clusters.get(net) or {}
        self.cids = sorted(cl)

        def put(bbox, e):
            c = _BUCKET
            for iy in range(int(math.floor(bbox[1] / c)), int(math.floor(bbox[3] / c)) + 1):
                for ix in range(int(math.floor(bbox[0] / c)), int(math.floor(bbox[2] / c)) + 1):
                    st.setdefault((ix, iy), []).append(e)
        for cid in self.cids:
            c = cl[cid]
            for p in c["pads"]:
                for lay in sorted(p.shapes):
                    for s in p.shapes[lay]:
                        put(s.bbox, (cid, 0, lay, s))
            for _n, lay, x1, y1, x2, y2, w in c["tracks"]:
                put((min(x1, x2) - w / 2, min(y1, y2) - w / 2, max(x1, x2) + w / 2, max(y1, y2) + w / 2),
                    (cid, 1, lay, (x1, y1, x2, y2, w / 2)))
            for _n, x, y, size, _drill, top, bot in c["vias"]:
                put((x - size / 2, y - size / 2, x + size / 2, y + size / 2), (cid, 2, (top, bot), (x, y, size / 2)))
            for lay, pts in c["islands"]:
                put(geom.poly_bbox(pts), (cid, 3, lay, pts))

    def static_at(self, layer: int, x: float, y: float) -> list[int]:
        """Cluster ids whose static copper contains the point on ``layer`` (-1 = any layer: a through via)."""
        out = []
        c = _BUCKET
        for e in self.static.get((int(math.floor(x / c)), int(math.floor(y / c))), ()):
            cid, kind, lay, d = e
            if cid in out:
                continue
            if kind == 2:
                if (layer < 0 or lay[0] <= layer <= lay[1]) and math.hypot(x - d[0], y - d[1]) <= d[2] + TEE_TOL:
                    out.append(cid)
            elif layer >= 0 and lay != layer:
                continue
            elif kind == 0:
                if d.dist_pt(x, y) <= TEE_TOL:
                    out.append(cid)
            elif kind == 1:
                if geom.pt_seg_dist(x, y, d[0], d[1], d[2], d[3]) <= d[4] + TEE_TOL:
                    out.append(cid)
            elif geom.pt_in_poly(d, x, y):
                out.append(cid)
        return out

    def _link(self, a, b, strict):
        adj = self.adj
        ea = adj.setdefault(a, {})
        if strict or b not in ea:
            ea[b] = strict
            adj.setdefault(b, {})[a] = strict

    def add(self, p: Piece):
        pid = p.id
        self.pieces[pid] = p
        self.adj.setdefault(pid, {})
        cells = _cells(p.x1, p.y1, p.x2, p.y2, p.w / 2 + TEE_TOL)
        b = self.buckets
        seen = set()
        pieces = self.pieces
        for k in cells:
            s = b.get(k)
            if s:
                for o in s:
                    if o not in seen:
                        seen.add(o)
                        r = _touch(p, pieces[o])
                        if r is not None:
                            self._link(pid, o, r)
                s.add(pid)
            else:
                b[k] = {pid}
        if p.kind == "via":
            for cid in self.static_at(-1, p.x1, p.y1):
                self._link(pid, -cid, True)
        else:
            for x, y in ((p.x1, p.y1), (p.x2, p.y2)):
                for cid in self.static_at(p.layer, x, y):
                    self._link(pid, -cid, True)
        self.comp.clear()
        self.version += 1

    def remove(self, p: Piece):
        pid = p.id
        if self.pieces.pop(pid, None) is None:
            return
        b = self.buckets
        for k in _cells(p.x1, p.y1, p.x2, p.y2, p.w / 2 + TEE_TOL):
            s = b.get(k)
            if s is not None:
                s.discard(pid)
                if not s:
                    del b[k]
        adj = self.adj
        for o in adj.pop(pid, ()):
            e = adj.get(o)
            if e is not None:
                e.pop(pid, None)
        self.comp.clear()
        self.version += 1

    def components(self, loose: bool) -> dict:
        """{node: component index}; components are numbered in canonical order (clusters, then piece ids)."""
        comp = self.comp.get(loose)
        if comp is not None:
            return comp
        comp = {}
        adj = self.adj
        n = 0
        for start in [-c for c in self.cids] + sorted(self.pieces):
            if start in comp:
                continue
            comp[start] = n
            stack = [start]
            while stack:
                u = stack.pop()
                for v, strict in (adj.get(u) or {}).items():
                    if v not in comp and (strict or loose):
                        comp[v] = n
                        stack.append(v)
            n += 1
        self.comp[loose] = comp
        return comp

    def ends_at(self, x: float, y: float, layers, loose: bool) -> list[int]:
        """Piece ids with an end at the point (strict: within TEE_TOL; loose: the point inside their copper)."""
        out = []
        c = _BUCKET
        for pid in sorted(self.buckets.get((int(math.floor(x / c)), int(math.floor(y / c))), ())):
            p = self.pieces[pid]
            if p.kind == "via":
                d = math.hypot(p.x1 - x, p.y1 - y)
                if d <= (p.w / 2 if loose else TEE_TOL):
                    out.append(pid)
            elif not layers or p.layer in layers:
                if loose:
                    if geom.pt_seg_dist(x, y, p.x1, p.y1, p.x2, p.y2) <= p.w / 2:
                        out.append(pid)
                elif min(math.hypot(p.x1 - x, p.y1 - y), math.hypot(p.x2 - x, p.y2 - y)) <= TEE_TOL:
                    out.append(pid)
        return out


class RouteState:
    """Transactional copper of one routing run. See the module docstring."""

    def __init__(self, oracle: Oracle, conns: list[ConnV2]):
        self.oracle = oracle
        self.board = oracle.b
        self.conns = conns            # the caller's list (classify may fill it later); looked up by ConnV2.idx
        self._by_idx = {}
        self._n_conns = -1
        self._pieces = {}             # id -> Piece
        self._by_conn = {}            # conn idx -> set of ids
        self._by_net = {}             # net -> set of ids
        self._status = {}             # conn idx -> status (missing = 'todo')
        self._fail = {}               # conn idx -> failure record of a 'failed' connection
        self._next = 1
        self._new_errors = 0
        self._log = []                # undo log (only while a transaction is open)
        self._marks = []              # log length at each open begin()
        # score aggregates (integers: exact under any add / remove order)
        self._len_nm = 0
        self._neck_nm = 0
        self._vias = 0
        self._n_routed = 0
        self._bends = {}              # conn idx -> cached bend count
        self._bend_total = 0
        self._bend_dirty = set()
        self._graphs = {}             # net -> _NetGraph
        self._pads = None             # pad name -> [Pad]

    # ── transactions ──────────────────────────────────────────────────────
    def begin(self) -> int:
        """Open a (nested) transaction; the token goes to commit() or rollback()."""
        self._marks.append(len(self._log))
        return len(self._marks)

    def _close(self, tok: int) -> int:
        if not isinstance(tok, int) or tok < 1 or tok > len(self._marks):
            raise ValueError(f"transaction token {tok!r} is not open (depth {len(self._marks)})")
        mark = self._marks[tok - 1]
        del self._marks[tok - 1:]
        return mark

    def commit(self, tok: int) -> None:
        """Keep everything done since ``begin`` (inner transactions still open are committed with it).
        The changes stay undoable by an enclosing transaction."""
        self._close(tok)
        if not self._marks:
            self._log.clear()

    def rollback(self, tok: int) -> None:
        """Undo everything done since ``begin`` returned ``tok`` (and close inner transactions)."""
        mark = self._close(tok)
        log = self._log
        while len(log) > mark:
            e = log.pop()
            k = e[0]
            if k == "add":
                self._delete(e[1])
            elif k == "rem":
                self._insert(e[1])
            elif k == "st":
                self._put_status(e[1], e[2], e[3])
            else:
                self._new_errors = e[1]
        if not self._marks:
            log.clear()

    @property
    def depth(self) -> int:
        """Number of open transactions."""
        return len(self._marks)

    # ── pieces ────────────────────────────────────────────────────────────
    def _graph(self, net: str) -> _NetGraph:
        g = self._graphs.get(net)
        if g is None:
            g = self._graphs[net] = _NetGraph(self.board, net)
        return g

    def _insert(self, p: Piece) -> None:
        self.oracle.add_piece(p)
        self._pieces[p.id] = p
        self._by_conn.setdefault(p.conn, set()).add(p.id)
        self._by_net.setdefault(p.net, set()).add(p.id)
        if p.kind == "via":
            self._vias += 1
        else:
            n = int(round(p.length * _NM))
            self._len_nm += n
            if p.w < p.target_w - 1e-9:
                self._neck_nm += n
        self._graph(p.net).add(p)
        self._dirty(p.conn)

    def _delete(self, p: Piece) -> None:
        self.oracle.remove_piece(p)
        del self._pieces[p.id]
        self._by_conn[p.conn].discard(p.id)
        self._by_net[p.net].discard(p.id)
        if p.kind == "via":
            self._vias -= 1
        else:
            n = int(round(p.length * _NM))
            self._len_nm -= n
            if p.w < p.target_w - 1e-9:
                self._neck_nm -= n
        self._graph(p.net).remove(p)
        self._dirty(p.conn)

    def _dirty(self, conn: int) -> None:
        if conn not in self._bend_dirty:
            self._bend_total -= self._bends.pop(conn, 0)
            self._bend_dirty.add(conn)

    def add(self, **piece_fields) -> Piece:
        """Add one piece: the fields of ``types.Piece`` without ``id`` (net, conn, kind, layer, x1, y1, x2, y2,
        w, drill, stage, protected, target_w). A via needs only x1, y1 (layer -1). Coordinates and sizes are
        quantised to 1 nm; the piece is added to the oracle's dynamic layer. Legality is the caller's job
        (``oracle.seg_ok`` / ``via_ok`` before, or ``oracle.piece_ok(piece)`` after)."""
        f = dict(piece_fields)
        unknown = [k for k in f if k not in _PIECE_FIELDS]
        if unknown:
            raise TypeError(f"RouteState.add: unknown piece field(s) {unknown}")
        kind = f.get("kind", "seg")
        f["kind"] = kind
        f.setdefault("conn", -1)
        f["x1"], f["y1"] = q(f["x1"]), q(f["y1"])
        if kind == "via":
            f["x2"], f["y2"] = f["x1"], f["y1"]
            f["layer"] = -1
            f["drill"] = q(f.get("drill", 0.0))
        else:
            f["x2"], f["y2"] = q(f["x2"]), q(f["y2"])
        f["w"] = q(f["w"])
        if f.get("target_w"):
            f["target_w"] = q(f["target_w"])
        p = Piece(id=self._next, **f)
        self._next += 1
        self._insert(p)
        if self._marks:
            self._log.append(("add", p))
        return p

    def remove(self, pid: int) -> None:
        """Remove one piece by id (KeyError when it is not in the state)."""
        p = self._pieces[pid]
        self._delete(p)
        if self._marks:
            self._log.append(("rem", p))

    def rip_conn(self, conn: int, *, protected: bool = False) -> list[Piece]:
        """Remove the pieces of one connection and set it back to 'todo'. Protected pieces (escape stubs,
        dog-bones) stay unless ``protected=True``. Returns the removed pieces, by id."""
        out = []
        for pid in sorted(self._by_conn.get(conn, ())):
            p = self._pieces[pid]
            if p.protected and not protected:
                continue
            self.remove(pid)
            out.append(p)
        if conn >= 0 and self.status(conn) != "todo":
            self.set_status(conn, "todo")
        return out

    def pieces(self, conn: int | None = None, net: str | None = None) -> list[Piece]:
        """Pieces by id: all, of one connection, or of one net."""
        P = self._pieces
        if conn is not None:
            ids = self._by_conn.get(conn, ())
            return [P[i] for i in sorted(ids) if net is None or P[i].net == net]
        if net is not None:
            return [P[i] for i in sorted(self._by_net.get(net, ()))]
        return [P[i] for i in sorted(P)]

    def piece(self, pid: int) -> Piece | None:
        return self._pieces.get(pid)

    def __len__(self):
        return len(self._pieces)

    def __bool__(self):
        return True                     # an empty state is still a state ("if ctx.state:" must not depend on its copper)

    # ── connection status ─────────────────────────────────────────────────
    def conn(self, idx: int) -> ConnV2:
        if self._n_conns != len(self.conns):
            self._by_idx = {c.idx: c for c in self.conns}
            self._n_conns = len(self.conns)
        return self._by_idx[idx]

    def _put_status(self, conn, status, fail):
        old = self._status.get(conn, "todo")
        if old == "routed":
            self._n_routed -= 1
        if status == "routed":
            self._n_routed += 1
        if status == "todo":
            self._status.pop(conn, None)
        else:
            self._status[conn] = status
        if fail is None:
            self._fail.pop(conn, None)
        else:
            self._fail[conn] = fail

    def set_status(self, conn: int, status: str, fail: dict | None = None) -> None:
        """status: 'todo' | 'routed' | 'failed'; ``fail`` is the failure record kept with a 'failed' connection."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, not {status!r}")
        if self._marks:
            self._log.append(("st", conn, self._status.get(conn, "todo"), self._fail.get(conn)))
        self._put_status(conn, status, fail if status == "failed" else None)

    def status(self, conn: int) -> str:
        return self._status.get(conn, "todo")

    def fail(self, conn: int) -> dict | None:
        """The failure record given to set_status(conn, 'failed', fail)."""
        return self._fail.get(conn)

    @property
    def new_errors(self) -> int:
        """New DRC errors of this state as last measured by the judge (part of the score)."""
        return self._new_errors

    @new_errors.setter
    def new_errors(self, n: int) -> None:
        if self._marks:
            self._log.append(("err", self._new_errors))
        self._new_errors = int(n)

    # ── connectivity ──────────────────────────────────────────────────────
    def _pad(self, name: str, x: float, y: float, net: str):
        if self._pads is None:
            self._pads = {}
            for p in self.board.pads:
                self._pads.setdefault(p.name, []).append(p)
        nid = self.oracle.net_id(net)
        best = None
        for p in self._pads.get(name, ()):
            if p.net != nid:
                continue
            d = abs(p.x - x) + abs(p.y - y)
            if best is None or d < best[0]:
                best = (d, p)
        return best[1] if best else None

    def _nodes(self, g: _NetGraph, net: str, t: Terminal, loose: bool) -> list[int]:
        cid = t.cluster
        if not (cid and cid in g.cids) and t.pad:
            p = self._pad(t.pad, t.x, t.y, net)
            cid = p.cluster if p is not None else 0
        if cid and cid in g.cids and not t.escape:
            return [-cid]
        out = [-cid] if (cid and cid in g.cids) else []
        # a bare point (tee point, escape end): the pieces that end there and the static copper under it
        out += g.ends_at(t.x, t.y, t.layers, loose)
        for lay in (t.layers or range(g.L)):
            out += [-c for c in g.static_at(lay, t.x, t.y) if -c not in out]
        return out

    def connected(self, conn: int, loose: bool = False) -> bool:
        """Is terminal a of the connection joined to terminal b (an open terminal b: to any other cluster of
        the net) through pieces of its net — of any connection — and the board's static clusters?
        ``loose``: also count joins where an end merely lies inside the other piece's copper (see module doc)."""
        c = self.conn(conn)
        g = self._graph(c.net)
        comp = g.components(loose)
        na = [n for n in self._nodes(g, c.net, c.a, loose) if n in comp]
        if not na:
            return False
        ca = {comp[n] for n in na}
        if is_open(c.b):
            mine = {n for n in na if n < 0}
            return any(n < 0 and n not in mine and k in ca for n, k in comp.items())
        return any(comp.get(n) in ca for n in self._nodes(g, c.net, c.b, loose))

    def net_groups(self, net: str, loose: bool = False) -> list[tuple[list[int], list[int]]]:
        """The net's connectivity groups that hold pieces: [(cluster ids, piece ids)], both sorted,
        groups in canonical order. Groups made of static clusters only are left out."""
        g = self._graph(net)
        comp = g.components(loose)
        by = {}
        for n, k in comp.items():
            by.setdefault(k, ([], []))[0 if n < 0 else 1].append(-n if n < 0 else n)
        return [(sorted(cs), sorted(ps)) for _k, (cs, ps) in sorted(by.items()) if ps]

    def dangling(self, net: str, loose: bool = False) -> list[int]:
        """'routed' connections of the net that are not connected() any more (after a rip of copper they
        teed onto), by idx."""
        out = []
        for c in self.conns:
            if c.net == net and self._status.get(c.idx) == "routed" and not self.connected(c.idx, loose):
                out.append(c.idx)
        return sorted(out)

    # ── snapshots ─────────────────────────────────────────────────────────
    def snapshot(self) -> Snapshot:
        P = self._pieces
        return Snapshot(tuple(P[i] for i in sorted(P)), tuple(sorted(self._status.items())),
                        tuple((k, self._fail[k]) for k in sorted(self._fail)), self._new_errors)

    def restore(self, snap: Snapshot) -> None:
        """Make the state equal to the snapshot: remove what it does not hold, add back what is missing
        (with the original ids), reset every status. Undoable like any other edit."""
        want = {p.id: p for p in snap.pieces}
        for pid in sorted(self._pieces):
            if pid not in want:
                self.remove(pid)
        for pid in sorted(want):
            if pid not in self._pieces:
                p = want[pid]
                self._insert(p)
                if self._marks:
                    self._log.append(("add", p))
        st = dict(snap.status)
        fails = dict(snap.fails)
        for conn in sorted(set(self._status) | set(st)):
            s, f = st.get(conn, "todo"), fails.get(conn)
            if self._status.get(conn, "todo") != s or self._fail.get(conn) is not f:
                self.set_status(conn, s, f)
        if self._new_errors != snap.new_errors:
            self.new_errors = snap.new_errors
        self._next = max(self._next, (snap.pieces[-1].id + 1) if snap.pieces else 1)

    # ── score ─────────────────────────────────────────────────────────────
    def _conn_bends(self, conn: int) -> int:
        ends = {}
        P = self._pieces
        for pid in sorted(self._by_conn.get(conn, ())):
            p = P[pid]
            if p.kind != "seg":
                continue
            dx, dy = p.x2 - p.x1, p.y2 - p.y1
            ends.setdefault((p.layer, p.x1, p.y1), []).append((dx, dy))
            ends.setdefault((p.layer, p.x2, p.y2), []).append((-dx, -dy))
        n = 0
        for v in ends.values():
            if len(v) == 2:
                (ax, ay), (bx, by) = v
                cross = ax * by - ay * bx
                la, lb = math.hypot(ax, ay), math.hypot(bx, by)
                if la > 0 and lb > 0 and (abs(cross) > 1e-6 * la * lb or ax * bx + ay * by > 0):
                    n += 1
        return n

    def score(self) -> Score:
        """Score of the current state: (new_errors, opens, necked_mm, vias, length_mm, bends).
        opens = connections not 'routed'; necked_mm = length of segments thinner than their target width;
        bends = corners where exactly two segments of one connection meet at an angle."""
        if self._bend_dirty:
            for conn in sorted(self._bend_dirty):
                n = self._conn_bends(conn)
                if n:
                    self._bends[conn] = n
                    self._bend_total += n
            self._bend_dirty.clear()
        return Score(new_errors=self._new_errors, opens=max(0, len(self.conns) - self._n_routed),
                     necked_mm=self._neck_nm / _NM, vias=self._vias, length_mm=self._len_nm / _NM,
                     bends=self._bend_total)

    # ── output ────────────────────────────────────────────────────────────
    def export(self, only=None) -> tuple[list, list, list[int]]:
        """-> (tracks, vias, piece ids) for the worker's ``commit`` op, in piece-id order:
        tracks ``[net, layer name, x1, y1, x2, y2, width]``, vias ``[net, x, y, size, drill]``; the id list is
        the track ids followed by the via ids, i.e. the order of ``track_uuids + via_uuids`` the commit
        returns. Segments shorter than 1 nm are left out. ``only``: restrict to these piece ids."""
        layers = self.board.layers
        only = None if only is None else set(only)
        tracks, vias, tid, vid = [], [], [], []
        P = self._pieces
        for pid in sorted(P):
            if only is not None and pid not in only:
                continue
            p = P[pid]
            if p.kind == "via":
                vias.append([p.net, p.x1, p.y1, p.w, p.drill])
                vid.append(pid)
            elif abs(p.x2 - p.x1) >= 1e-6 or abs(p.y2 - p.y1) >= 1e-6:
                tracks.append([p.net, layers[p.layer], p.x1, p.y1, p.x2, p.y2, p.w])
                tid.append(pid)
        return tracks, vias, tid + vid
