"""Router v2 fan-out (design §4.5, §10): QFN 0.4 mm at-the-rule escape legal; slot order = pad order (no
crossings); over-capacity ring -> via escape; boxed pad reported with its Cut. Plus rows, lanes, the flow
assignment, escape terminals, sync / undo / re_escape, plan styles, determinism, budget.

Synthetic boards (no KiCad) for everything; with KiCad 10 and the benchmark corpus the escapes of real boards
are committed to a scratch copy and judged by kicad-cli DRC.

Run:  python tests/test_v2_fanout.py                 pure tests + 2 corpus boards
      python tests/test_v2_fanout.py --full          ... + every corpus board
      python tests/test_v2_fanout.py test_name       one test
Scratch files go to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench" / "route"))
import common  # noqa: E402  (bench/route/common.py)

common.sandbox()       # KICAD_CONFIG_HOME, COPPER_HOME and the temp dir -> bench/route/.work, before copper_kicad loads
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import geom, model  # noqa: E402
from copper_kicad.pcb.v2 import fanout as F  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Ctx, Cut, Piece, Score, Terminal  # noqa: E402

FULL = "--full" in sys.argv or os.environ.get("COPPER_V2_FANOUT_FULL") == "1"
UM = 0.001


# ── a minimal RouteState with the §9 interface (pieces.py may still be a stub) ──
class MiniState:
    def __init__(self, oracle, conns):
        self.o = oracle
        self._p = {}
        self._next = 1
        self._st = {c.idx: "todo" for c in conns}
        self.fails = {}
        self._log = []
        self._marks = []
        self.new_errors = 0

    def begin(self):
        self._marks.append(len(self._log))
        return len(self._marks)

    def commit(self, tok):
        self._marks.pop()

    def rollback(self, tok):
        mark = self._marks.pop()
        while len(self._log) > mark:
            op, p = self._log.pop()
            if op == "add":
                del self._p[p.id]
                self.o.remove_piece(p)
            else:
                self._p[p.id] = p
                self.o.add_piece(p)

    def add(self, **f):
        p = Piece(id=self._next, **f)
        self._next += 1
        self._p[p.id] = p
        self.o.add_piece(p)
        self._log.append(("add", p))
        return p

    def remove(self, pid):
        p = self._p.pop(pid)
        self.o.remove_piece(p)
        self._log.append(("del", p))

    def rip_conn(self, conn):
        out = [p for p in self.pieces(conn=conn)]
        for p in out:
            self.remove(p.id)
        return out

    def pieces(self, conn=None, net=None):
        return [p for _i, p in sorted(self._p.items()) if (conn is None or p.conn == conn) and (net is None or p.net == net)]

    def set_status(self, conn, status, fail=None):
        self._st[conn] = status
        if fail is not None:
            self.fails[conn] = fail

    def status(self, conn):
        return self._st[conn]

    def score(self):
        return Score(opens=sum(1 for s in self._st.values() if s != "routed"))


def real_state():
    """pieces.RouteState when W1's file has landed, else None."""
    try:
        from copper_kicad.pcb.v2 import pieces
    except Exception:  # noqa: BLE001
        return None
    return None if getattr(pieces, "STUB", False) else pieces.RouteState


STATES = [MiniState] + ([real_state()] if real_state() else [])


# ── synthetic boards ──────────────────────────────────────────────────────
RULES = {"clearance": 0.1, "track_width": 0.1, "via": 0.4, "drill": 0.2, "edge_clearance": 0.3, "hole_clearance": 0.25,
         "hole_to_hole": 0.25, "annular": 0.05,
         "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.25, "dp_width": 0.2}}


def P(ref, num, net, x, y, hw, hh, r=0.0, rot=0.0, kind="smd", layer=0, hole=0.0):
    return {"ref": ref, "num": str(num), "net": net, "x": round(x, 6), "y": round(y, 6), "hw": hw, "hh": hh, "r": r,
            "rot": rot, "kind": kind, "layer": layer, "hole": hole}


def mkboard(pads, size=(60.0, 60.0), layers=("F.Cu", "B.Cu"), nets=None, rules=None, tracks=(), vias=(), zones=()):
    names = sorted({p["net"] for p in pads if p.get("net")} | set(nets or {}) | {t[0] for t in tracks} | {v[0] for v in vias})
    R = dict(RULES)
    R.update(rules or {})
    df = R["default"]
    ex = {"layers": list(layers), "rules": R,
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": df["clearance"], "width": df["width"],
                       "via": df["via"], "drill": df["drill"], "dp_gap": 0.25, "dp_width": 0.2,
                       **((nets or {}).get(n) or {})} for i, n in enumerate(names)},
          "outline": {"closed": True, "polys": [{"o": [[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [list(t) for t in tracks],
          "vias": [list(v) for v in vias], "zones": [dict(z) for z in zones], "mask_openings": [],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    allL = list(range(len(layers)))
    for i, p in enumerate(pads):
        tht = p["kind"] == "tht"
        lays = allL if tht else [p["layer"]]
        a = math.radians(p["rot"])
        d = {"ref": p["ref"], "num": p["num"], "net": p["net"], "x": p["x"], "y": p["y"], "kind": p["kind"],
             "layers": lays, "c": i + 1,
             "shapes": [[lays, "box", [p["x"], p["y"], round(math.cos(a), 9), round(math.sin(a), 9), p["hw"], p["hh"], p["r"]]]]}
        if p["hole"]:
            d["hole"] = [p["x"], p["y"], 1.0, 0.0, p["hole"], p["hole"]]
        ex["pads"].append(d)
    return model.Board(ex)


def _rot(cx, cy, x, y, deg):
    a = math.radians(deg)
    dx, dy = x - cx, y - cy
    return cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a)


def quad(ref, cx, cy, n, pitch, pw, pl, span, net=lambda i: f"N{i}", rot=0.0, ep=None, layer=0, sides="WSEN"):
    """QFP / QFN: n pads per side, pad pw (along the row) x pl (along the exit), pad centres span / 2 from the
    centre; numbered 1.. counter-clockwise from the top of the west side."""
    out = []
    k = 0
    h = (n - 1) * pitch / 2
    for side in "WSEN":
        for i in range(n):
            k += 1
            if side not in sides:
                continue
            if side == "W":
                x, y, hw, hh = cx - span / 2, cy - h + i * pitch, pl / 2, pw / 2
            elif side == "S":
                x, y, hw, hh = cx - h + i * pitch, cy + span / 2, pw / 2, pl / 2
            elif side == "E":
                x, y, hw, hh = cx + span / 2, cy + h - i * pitch, pl / 2, pw / 2
            else:
                x, y, hw, hh = cx + h - i * pitch, cy - span / 2, pw / 2, pl / 2
            x, y = _rot(cx, cy, x, y, rot)
            out.append(P(ref, k, net(k), x, y, hw, hh, rot=rot, layer=layer))
    if ep:
        out.append(P(ref, k + 1, "GND", cx, cy, ep / 2, ep / 2, rot=rot, layer=layer))
    return out


def dual(ref, cx, cy, n, pitch, pw, pl, span, net=lambda i: f"S{i}"):
    """SOIC: two columns of n pads (west 1..n top to bottom, east n+1.. bottom to top)."""
    out = []
    h = (n - 1) * pitch / 2
    for i in range(n):
        out.append(P(ref, i + 1, net(i + 1), cx - span / 2, cy - h + i * pitch, pl / 2, pw / 2))
    for i in range(n):
        out.append(P(ref, n + i + 1, net(n + i + 1), cx + span / 2, cy + h - i * pitch, pl / 2, pw / 2))
    return out


def bga(ref, cx, cy, nx, ny, pitch, d, net=lambda i: f"B{i}"):
    out = []
    k = 0
    for j in range(ny):
        for i in range(nx):
            k += 1
            out.append(P(ref, k, net(k), cx + (i - (nx - 1) / 2) * pitch, cy + (j - (ny - 1) / 2) * pitch, d / 2, d / 2,
                         r=d / 2))
    return out


def tp(name, net, x, y, kind="tht", layer=0):
    """A partner pad: a through-hole test point (every layer) or an SMD one on ``layer``."""
    if kind == "tht":
        return P(name, 1, net, x, y, 0.5, 0.5, r=0.5, kind="tht", hole=0.3)
    return P(name, 1, net, x, y, 0.4, 0.4, layer=layer)


def conns_for(board, pairs, role="signal", **kw):
    """pairs: (pad name a, pad name b) -> ConnV2 list with pad terminals (widths left to the net class)."""
    by = {}
    for p in board.pads:
        by.setdefault(p.name, p)
    out = []
    for i, (a, b) in enumerate(pairs):
        pa, pb = by[a], by[b]
        net = board.net_by_id[pa.net].name
        out.append(ConnV2(idx=i, net=net, a=Terminal(pa.x, pa.y, tuple(pa.layers), pad=pa.name, cluster=pa.cluster),
                          b=Terminal(pb.x, pb.y, tuple(pb.layers), pad=pb.name, cluster=pb.cluster),
                          est=math.hypot(pa.x - pb.x, pa.y - pb.y), role=role, rank=(i,), **kw))
    return out


def mkctx(board, conns, state=MiniState, **x):
    orc = O.Oracle(board)
    xx = SimpleNamespace(fanout="auto", fanout_parts={})
    for k, v in x.items():
        setattr(xx, k, v)
    return Ctx(board=board, cfg=None, x=xx, oracle=orc, state=state(orc, conns), conns=conns)


def verify(ctx):
    """Independent judgement: a fresh oracle, every piece checked against the board and all earlier pieces."""
    o = O.Oracle(ctx.board)
    bad = []
    for p in ctx.state.pieces():
        b = o.piece_ok(p)
        if b is not None:
            bad.append((p, b))
        o.add_piece(p)
    return bad


def ring_out(n, pitch, r_in, r_out, cx=30.0, cy=30.0, lat=None, stagger=0.0):
    """Partner positions for a quad part: pad i of a side gets a test point out of that side, ``lat`` times
    as far from the side's middle as the pad (default r_out / r_in: a radial fan; 1.0: straight ahead, then
    ``stagger`` keeps neighbouring test points apart)."""
    k = r_out / r_in if lat is None else lat

    def at(side, i):
        o = (i - (n - 1) / 2) * pitch * k
        r = r_out + (i % 4) * stagger
        if side == "W":
            return cx - r, cy + o
        if side == "S":
            return cx + o, cy + r
        if side == "E":
            return cx + r, cy - o
        return cx - o, cy - r
    return at


def qfn_ctx(n=8, pitch=0.4, pw=0.2, pl=0.8, span=4.0, ep=2.4, state=MiniState, partner="tht", rot=0.0, far=14.0,
            lat=1.0, **x):
    """A QFN (default: 32 pads, 0.4 mm pitch, 0.2 mm pads: a 0.2 mm track at 0.2 mm clearance is exactly at the
    rule between neighbouring pads) with one partner test point per pad, straight ahead of it (lat = 1) or
    fanned out radially (lat = None)."""
    pads = quad("U1", 30.0, 30.0, n, pitch, pw, pl, span, rot=rot, ep=ep)
    at = ring_out(n, pitch, span / 2, far, lat=lat, stagger=1.6)
    pairs = []
    k = 0
    for side in "WSEN":
        for i in range(n):
            k += 1
            x_, y_ = _rot(30.0, 30.0, *at(side, i), rot)
            pads.append(tp(f"TP{k}", f"N{k}", x_, y_, kind=partner, layer=1))
            pairs.append((f"U1.{k}", f"TP{k}.1"))
    b = mkboard(pads, size=(64.0, 64.0))
    return mkctx(b, conns_for(b, pairs), state=state, **x)


def lanes_of(ctx, ref):
    """Per row: [(pad lateral coordinate, slot lateral coordinate, ring)] of the escaped pads, in pad order."""
    out = []
    reg = F.registry(ctx)
    for row in F.footprint_rows(ctx.board, ref):
        ls = []
        for p in row.pads:
            rec = reg["by_pad"].get((p.name, round(p.x, 4), round(p.y, 4)))
            if rec is not None and rec["lane"] is not None:
                ls.append((rec["lane"]["t"], rec["lane"]["lane"], rec["lane"]["b"]))
        out.append(ls)
    return out


def crossings(ctx):
    """Pairs of fan-out segments of different nets on one layer that cross."""
    segs = [p for p in ctx.state.pieces() if p.kind == "seg"]
    bad = []
    for i, a in enumerate(segs):
        for b in segs[i + 1:]:
            if a.net != b.net and a.layer == b.layer and \
                    geom.segs_cross((a.x1, a.y1), (a.x2, a.y2), (b.x1, b.y1), (b.x2, b.y2)):
                bad.append((a, b))
    return bad


# ── lattice, rows, needs_fanout ───────────────────────────────────────────
def test_lattice_pitch_follows_the_window_ladder():
    assert F.lattice_pitch(0.5, 0.2, 0.2) == 0.125          # 0.5 = 4 x 0.125, p <= 0.2
    assert F.lattice_pitch(0.4, 0.2, 0.2) == 0.2            # 0.4 = 2 x 0.2
    assert F.lattice_pitch(0.65, 0.2, 0.2) == 0.05          # 0.65 = 13 x 0.05, nothing coarser divides it
    assert F.lattice_pitch(1.27, 0.25, 0.2) == 0.2          # nothing divides 1.27: p <= min(0.225, 1.27 / 4)
    assert F.lattice_pitch(0.4, 0.1, 0.1) == 0.1            # p <= (w + cl) / 2
    assert F.lattice_pitch(0.5, 0.09, 0.09) == 0.05
    assert F.lattice_pitch(0.0, 0.2, 0.2) == 0.2            # a lone pad: only rule (a)


def test_rows_of_a_qfp():
    b = mkboard(quad("U1", 30, 30, 16, 0.5, 0.3, 1.5, 11.0))
    rows = F.footprint_rows(b, "U1")
    assert len(rows) == 4 and all(len(r.pads) == 16 and not r.grid and not r.ambiguous and r.smd for r in rows)
    by_n = {(round(r.n[0]), round(r.n[1])): r for r in rows}
    assert set(by_n) == {(-1, 0), (1, 0), (0, -1), (0, 1)}
    w = by_n[(-1, 0)]
    assert [p.num for p in w.pads] == [str(i) for i in range(1, 17)]            # ordered along the row
    assert abs(w.pitch - 0.5) < 1e-9 and abs(w.pad_w - 0.3) < 1e-9 and abs(w.out_len - 0.75) < 1e-9
    s = by_n[(0, 1)]
    assert [p.num for p in s.pads] == [str(i) for i in range(17, 33)] and abs(s.t[0]) == 1.0
    assert F.footprint_rows(b, "U1") is rows                                    # cached
    assert F.footprint_rows(b, "NOPE") == []


def test_rows_soic_qfn_exposed_pad_and_small_parts():
    pads = dual("U2", 12, 12, 4, 1.27, 0.6, 1.5, 5.4) + quad("U3", 30, 30, 7, 0.5, 0.25, 0.8, 5.0, ep=3.2) + \
        [P("R1", 1, "A", 45, 45, 0.25, 0.3), P("R1", 2, "B", 46, 45, 0.25, 0.3)] + \
        [P("Q1", i + 1, f"Q{i}", 50 + (i % 2) * 2.0, 10 + (i // 2) * 1.9, 0.5, 0.35) for i in range(4)]
    b = mkboard(pads)
    r2 = F.footprint_rows(b, "U2")
    assert len(r2) == 2 and sorted(round(r.n[0]) for r in r2) == [-1, 1] and all(len(r.pads) == 4 for r in r2)
    r3 = F.footprint_rows(b, "U3")
    # 7 pads per side: the middle pad of each side lines up with the exposed pad, which must not join a row or
    # turn the part into a grid
    assert len(r3) == 4 and all(len(r.pads) == 7 and not r.grid for r in r3)
    assert all(p.num != "29" for r in r3 for p in r.pads)
    assert F.footprint_rows(b, "R1") == [] and F.footprint_rows(b, "Q1") == []
    # needs_fanout: pitch < 2 (w + cl) + pad_w, or >= 16 pads; never without a row
    assert F.needs_fanout(b, "U3", 0.2, 0.2)
    assert F.needs_fanout(b, "U2", 0.2, 0.2)                 # 1.27 < 0.8 + 0.6
    assert not F.needs_fanout(b, "U2", 0.1, 0.1)             # 1.27 >= 0.4 + 0.6 and only 8 pads
    assert not F.needs_fanout(b, "R1", 0.2, 0.2) and not F.needs_fanout(b, "Q1", 0.2, 0.2)
    assert not F.needs_fanout(b, "NOPE", 0.2, 0.2)


def test_rows_rotated_part_and_bga_grid():
    pads = quad("U1", 20, 20, 8, 0.5, 0.3, 1.2, 7.0, rot=45.0) + bga("U2", 45, 45, 5, 5, 0.8, 0.4) + \
        [P("J1", i + 1, f"J{i}", 10 + i * 1.0, 50, 0.3, 0.6) for i in range(6)]
    b = mkboard(pads)
    rows = F.footprint_rows(b, "U1")
    assert len(rows) == 4 and all(len(r.pads) == 8 for r in rows)
    for r in rows:
        assert abs(abs(r.n[0]) - math.sqrt(0.5)) < 1e-6 and abs(r.pitch - 0.5) < 1e-6
        c = sum(p.x for p in r.pads) / 8 - 20, sum(p.y for p in r.pads) / 8 - 20
        assert c[0] * r.n[0] + c[1] * r.n[1] > 3.0                              # the normal points away from the part
    g = F.footprint_rows(b, "U2")
    assert len(g) == 5 and all(r.grid and len(r.pads) == 5 and abs(r.pitch - 0.8) < 1e-9 and abs(r.pitch2 - 0.8) < 1e-9
                               for r in g)
    j = F.footprint_rows(b, "J1")
    assert len(j) == 1 and j[0].ambiguous and len(j[0].pads) == 6                # a single row has no outward side
    assert F.needs_fanout(b, "U2", 0.2, 0.2) and F.needs_fanout(b, "J1", 0.2, 0.2)


# ── the QFN at the rule ───────────────────────────────────────────────────
def test_qfn_04_at_the_rule_escapes_are_legal():
    for st in STATES:
        ctx = qfn_ctx(state=st)
        rep = F.run(ctx, Budget(10_000_000))
        assert rep.name == "fanout" and rep.routed == 32 and rep.failed == 0, rep.notes
        assert rep.data["parts"] == {"U1": {"escaped": 32, "ring": 32, "via": 0, "unescaped": 0, "boxed": 0}}
        assert verify(ctx) == []
        pcs = ctx.state.pieces()
        assert len(pcs) == 32 and all(p.kind == "seg" and p.stage == "fanout" and p.protected and p.conn == -1
                                      and p.w == 0.2 and p.layer == 0 for p in pcs)
        # at the rule: the stub passes its neighbours' pads with exactly the clearance (0.4 - 0.1 - 0.1 = 0.2);
        # one micron wider is a violation, and so is one micron sideways
        o = O.Oracle(ctx.board)
        p = next(p for p in pcs if p.net == "N3")
        assert o.seg_ok(0, p.x1, p.y1, p.x2, p.y2, 0.2, "N3") is None
        assert o.seg_ok(0, p.x1, p.y1, p.x2, p.y2, 0.202, "N3").rule == "clearance"
        assert o.seg_ok(0, p.x1, p.y1 + 0.001, p.x2, p.y2 + 0.001, 0.2, "N3").rule == "clearance"
        # every stub: from the pad centre straight out to the first ring, on the lattice of the pads
        for c in ctx.conns:
            assert c.a.escape and c.a.pad.startswith("U1.") and c.a.layers == (0,) and not c.b.escape
            rec = F.escape_of(ctx, c.a.pad)
            assert rec["kind"] == "ring" and rec["desc"] == {"ring": 1, "from": 1, "dir": 0}
            assert rec["terminal"] is c.a and len(rec["pieces"]) == 1
            pc = next(p for p in pcs if p.id == rec["pieces"][0])
            pad = next(p for p in ctx.board.pads if p.name == c.a.pad)
            assert (pc.x1, pc.y1) == (pad.x, pad.y) and (pc.x2, pc.y2) == (c.a.x, c.a.y)
            ln = math.hypot(pc.x2 - pc.x1, pc.y2 - pc.y1)
            assert abs(ln - 0.8) < 1e-9                       # pad half length 0.4 + (w + cl), a multiple of p = 0.2
        assert rep.work > 0 and rep.work == 32 * 200 + (ctx.oracle.checks // 8)


def test_qfn_rotated_45_escapes_are_legal():
    ctx = qfn_ctx(rot=45.0)
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.routed == 32 and verify(ctx) == [], rep.notes
    for p in ctx.state.pieces():
        d = (p.x2 - p.x1, p.y2 - p.y1)
        assert abs(abs(d[0]) - abs(d[1])) < 2e-6             # stubs leave along the rotated row normals


# ── planar order ──────────────────────────────────────────────────────────
def qfp_fan_ctx(state=MiniState, **x):
    """QFP-64 0.5 mm; the partners of each side sit on a line much wider than the row, so the outer pads'
    partners are off to the side and the escapes want to lean outwards."""
    n, pitch, span = 16, 0.5, 11.0
    pads = quad("U1", 30.0, 30.0, n, pitch, 0.3, 1.5, span)
    at = ring_out(n, pitch, span / 2, 22.0)
    pairs = []
    k = 0
    for side in "WSEN":
        for i in range(n):
            k += 1
            x_, y_ = at(side, i)
            pads.append(tp(f"TP{k}", f"N{k}", x_, y_))
            pairs.append((f"U1.{k}", f"TP{k}.1"))
    b = mkboard(pads, size=(60.0, 60.0))
    return mkctx(b, conns_for(b, pairs), state=state, **x)


def test_slot_order_equals_pad_order_and_nothing_crosses():
    for st in STATES:
        ctx = qfp_fan_ctx(state=st)
        rep = F.run(ctx, Budget(10_000_000))
        assert rep.routed == 64 and rep.failed == 0 and rep.data["unescaped"] == [], rep.notes
        assert verify(ctx) == [] and crossings(ctx) == []
        rows = lanes_of(ctx, "U1")
        assert len(rows) == 4
        legs = 0
        for ls in rows:
            assert len(ls) == 16
            slots = [l[1] for l in ls]
            assert slots == sorted(slots) and len(set(slots)) == 16            # slot order = pad order
            for a, b in zip(ls, ls[1:]):
                assert b[1] - a[1] >= 0.4 - 1e-9                              # every slot keeps a lane of w + cl
            legs += sum(1 for l in ls if l[2] > 1)
            # the fan opens outwards: the first pads lean to -t, the last to +t, the middle stays straight
            assert ls[0][1] < ls[0][0] and ls[-1][1] > ls[-1][0]
            assert ls[7][1] == ls[7][0] and ls[8][1] == ls[8][0]
        assert legs >= 8
        # escape terminals are the slot points, on the pad layer, in pad order around the part
        for c in ctx.conns:
            rec = F.escape_of(ctx, c.a.pad)
            assert c.a.escape and (c.a.x, c.a.y) == (rec["terminal"].x, rec["terminal"].y)


def test_lane_rule_units():
    def lane(t, slot, a, b, real=True):
        return {"t": t, "lane": slot, "a": a, "b": b, "w": 0.2, "cl": 0.2, "real": real}
    rs = 0.5
    straight0, straight1 = lane(0.0, 0.0, 1, 1), lane(0.5, 0.5, 1, 1)
    assert F._lane_ok(straight1, [straight0], rs)
    assert not F._lane_ok(lane(0.3, 0.3, 1, 1), [straight0], rs)                 # lanes closer than w + cl
    # a leg over the neighbour's exit: order broken
    assert not F._lane_ok(lane(0.0, 1.0, 1, 3), [straight1], rs)
    # the end pad leans away from the row: fine; its neighbour may then take the slot above the end pad
    left = lane(0.0, -1.0, 1, 3)
    assert F._lane_ok(left, [straight1], rs)
    assert F._lane_ok(lane(0.5, 0.0, 2, 3), [left], rs)
    assert not F._lane_ok(lane(0.5, 0.0, 1, 2), [lane(0.0, 0.0, 2, 2)], rs)     # slot under a stub that goes higher
    # a pad without an escape keeps its exit: no leg may pass over it, a neighbour's straight stub may stand next to it
    quiet = lane(0.5, 0.5, 0, 0, real=False)
    assert F._lane_ok(lane(0.0, 0.0, 1, 1), [quiet], rs)
    assert not F._lane_ok(lane(0.0, 1.0, 1, 3), [quiet], rs)
    assert not F._lane_ok(lane(1.0, 0.5, 1, 2), [quiet], rs)
    assert F._lane_ok(lane(1.0, 1.5, 1, 2), [quiet], rs)                         # leaning away is fine


# ── over capacity -> via escape ───────────────────────────────────────────
def dense_ctx(state=MiniState, layers=("F.Cu", "B.Cu"), pad_w=0.1, **x):
    """One row of 10 pads at 0.4 mm with a 0.25 mm / 0.2 mm class. With 0.1 mm pads a stub clears the
    neighbouring pads (0.4 - 0.05 - 0.125 = 0.225) but two neighbouring stubs do not clear each other
    (0.4 - 0.25 = 0.15): lanes of w + cl = 0.45 do not fit the pitch, the ring is over capacity."""
    pads = [P("J1", i + 1, f"N{i + 1}", 20.0 + i * 0.4, 30.0, pad_w / 2, 0.5) for i in range(10)]
    pads += [P("J1", 20 + i, "", 19.2 + i * 5.2, 33.0, 0.4, 0.4) for i in range(2)]      # mounting pads: the row is the north side
    pairs = []
    for i in range(10):
        pads.append(tp(f"TP{i + 1}", f"N{i + 1}", 12.0 + i * 2.0, 12.0))
        pairs.append((f"J1.{i + 1}", f"TP{i + 1}.1"))
    rules = dict(RULES)
    rules["default"] = dict(RULES["default"], width=0.25, via=0.5, drill=0.2)
    b = mkboard(pads, rules=rules, layers=layers)
    return mkctx(b, conns_for(b, pairs), state=state, **x)


def walled_ctx(state=MiniState, **x):
    """One row of 10 pads at 0.65 mm (north side of J1); a foreign track runs in front of pads 4..7 where the
    first ring would be: the ring cannot serve those four, there is room for vias behind the row."""
    pads = [P("J1", i + 1, f"N{i + 1}", 20.0 + i * 0.65, 30.0, 0.15, 0.5) for i in range(10)]
    pads += [P("J1", 20 + i, "", 18.5 + i * 8.85, 34.5, 0.4, 0.4) for i in range(2)]     # mounting pads: the row is the north side
    pairs = []
    for i in range(10):
        pads.append(tp(f"TP{i + 1}", f"N{i + 1}", 12.0 + i * 2.6, 12.0))
        pairs.append((f"J1.{i + 1}", f"TP{i + 1}.1"))
    rules = dict(RULES)
    rules["default"] = dict(RULES["default"], via=0.5, drill=0.2)
    wall = [("X", 0, 20.0 + 3 * 0.65, 29.0, 20.0 + 6 * 0.65, 29.0, 0.2, 0, False)]
    b = mkboard(pads, rules=rules, tracks=wall, nets={"X": {}})
    return mkctx(b, conns_for(b, pairs), state=state, **x)


def test_over_capacity_ring_goes_to_via_escape():
    for st in STATES:
        ctx = walled_ctx(state=st)
        row = F.footprint_rows(ctx.board, "J1")[0]
        assert len(row.pads) == 10 and (round(row.n[0]), round(row.n[1])) == (0, -1) and not row.ambiguous
        rep = F.run(ctx, Budget(10_000_000))
        st_ = rep.data["parts"]["J1"]
        assert st_ == {"escaped": 10, "ring": 6, "via": 4, "unescaped": 0, "boxed": 0}, (st_, rep.notes, rep.data["unescaped"])
        assert verify(ctx) == [] and crossings(ctx) == []
        kinds = [F.escape_of(ctx, f"J1.{i + 1}")["kind"] for i in range(10)]
        assert kinds == ["ring"] * 3 + ["via"] * 4 + ["ring"] * 3
        vias = [p for p in ctx.state.pieces() if p.kind == "via"]
        assert len(vias) == 4 and all(p.w == 0.5 and p.drill == 0.2 and p.protected and p.y1 > 30.5 for p in vias)
        for a in vias:
            for b in vias:
                if a.id < b.id:
                    assert math.hypot(a.x1 - b.x1, a.y1 - b.y1) >= 0.5 + 0.2 - 1e-6     # one via per column
        # 0.65 mm pitch, 0.7 mm via lattice: the four vias cannot stand in one line: the flow staggers the depths
        assert len({round(p.y1, 3) for p in vias}) >= 2
        for c in ctx.conns:
            rec = F.escape_of(ctx, c.a.pad)
            assert c.a.escape and rec is not None
            if rec["kind"] == "via":
                assert c.a.layers == (0, 1) and (c.a.x, c.a.y) == rec["via"] and rec["desc"]["dir"] == "in"
            else:
                assert c.a.layers == (0,)
        slots = [l[1] for l in lanes_of(ctx, "J1")[0]]
        assert slots == sorted(slots) and len(slots) == 6


def test_stubs_wider_than_the_pads_take_every_other_lane():
    """0.1 mm pads at 0.4 mm, 0.25 mm tracks: a stub clears the neighbouring pads but not the neighbouring
    stub (both start on their pad centres, 0.15 mm apart edge to edge). Every other pad gets the ring; the
    rest is left to the detail stage (not boxed: they can still leave from the far end of the pad)."""
    ctx = dense_ctx()
    rep = F.run(ctx, Budget(10_000_000))
    st = rep.data["parts"]["J1"]
    assert st == {"escaped": 5, "ring": 5, "via": 0, "unescaped": 5, "boxed": 0}, st
    assert verify(ctx) == [] and rep.failed == 0 and all(ctx.state.status(c.idx) == "todo" for c in ctx.conns)
    assert [u["pad"] for u in rep.data["unescaped"]] == [f"J1.{i}" for i in (2, 4, 6, 8, 10)]


def test_partner_on_the_other_layer_gets_a_via_only_where_nobody_is_boxed():
    """QFN 0.5 mm with an exposed pad, every partner on B.Cu, 0.6 mm vias: a via in front of a pad closes the
    exits of both neighbours, and there is no room inwards. Vias for every other pad would box the pads in
    between, so those vias are taken back: every pad keeps an exit."""
    ctx = qfn_ctx(n=6, pitch=0.5, pw=0.25, pl=0.8, span=5.0, ep=2.6, partner="smd")     # partners on B.Cu
    rep = F.run(ctx, Budget(10_000_000))
    st = rep.data["parts"]["U1"]
    assert st["escaped"] == 24 and st["boxed"] == 0 and st["unescaped"] == 0, (st, rep.data["unescaped"])
    assert st["via"] >= 4 and st["ring"] >= 12 and st["via"] + st["ring"] == 24
    assert verify(ctx) == [] and crossings(ctx) == []
    vias = [p for p in ctx.state.pieces() if p.kind == "via"]
    assert len(vias) == st["via"]
    # no via sits on an SMD pad
    for v in vias:
        for p in ctx.board.pads:
            if p.kind == "smd":
                for s in p.shapes[p.layers[0]]:
                    assert s.dist_pt(v.x1, v.y1) >= 0.3 * 0.6 - 1e-9
    # no outward via stands in front of a pad that has only a ring stub
    reg = F.registry(ctx)
    for row in F.footprint_rows(ctx.board, "U1"):
        recs = [reg["by_pad"][(p.name, round(p.x, 4), round(p.y, 4))] for p in row.pads]
        for r in recs:
            if r["kind"] != "via":
                continue
            lat = r["via"][0] * row.t[0] + r["via"][1] * row.t[1]
            for o in recs:
                if o["kind"] == "ring":
                    assert abs(lat - o["lane"]["lane"]) >= 0.3 + 0.2 + 0.1 - 1e-9
    # without the exposed pad there is room inwards: in / out alternate and every pad gets its via
    ctx = qfn_ctx(n=6, pitch=0.5, pw=0.25, pl=0.8, span=7.0, ep=0.0, partner="smd")
    rep = F.run(ctx, Budget(10_000_000))
    st = rep.data["parts"]["U1"]
    assert st["via"] == 24 and st["ring"] == 0 and verify(ctx) == [], (st, rep.data["unescaped"])
    dirs = {e["desc"]["dir"] for e in rep.data["escapes"]}
    assert dirs == {"in", "out"}


def test_flow_assignment_staggers_vias():
    """Four pads at 0.5 mm, 0.6 mm vias: neighbours cannot both take the nearest site; the flow spreads them
    over depths / sides at minimum total stub length and every pad gets a via."""
    pads = [P("J1", i + 1, f"N{i + 1}", 25.0 + i * 0.5, 30.0, 0.125, 0.5) for i in range(4)]
    pads += [P("J1", 9, "", 23.0, 33.0, 0.4, 0.4), P("J1", 10, "", 28.5, 33.0, 0.4, 0.4)]
    pairs = []
    for i in range(4):
        pads.append(tp(f"TP{i + 1}", f"N{i + 1}", 22.0 + i * 2.5, 15.0, kind="smd", layer=1))
        pairs.append((f"J1.{i + 1}", f"TP{i + 1}.1"))
    b = mkboard(pads)
    ctx = mkctx(b, conns_for(b, pairs))
    res = F.via_escape(ctx, "J1", [f"J1.{i + 1}" for i in range(4)])
    assert [r["ok"] for r in res] == [True] * 4, res
    assert verify(ctx) == []
    descs = [(r["desc"]["dir"], r["desc"]["depth"], r["desc"]["shift"]) for r in res]
    assert len(set(descs)) >= 2                                                 # not all on the same site pattern
    assert {d[0] for d in descs} == {"in", "out"}                               # 0.5 mm pitch: the sides alternate
    assert res[0]["terminal"].layers == (0, 1) and res[0]["terminal"].escape and ctx.conns[0].a is res[0]["terminal"]
    vias = [p for p in ctx.state.pieces() if p.kind == "via"]
    assert len(vias) == 4
    for a in vias:
        for c in vias:
            if a.id < c.id:
                assert math.hypot(a.x1 - c.x1, a.y1 - c.y1) >= 0.8 - 1e-6
    # same call again: nothing left to do
    again = F.via_escape(ctx, "J1", ["J1.1"])
    assert again[0]["ok"] is False and "skip" in again[0]


def test_pad_pair_cuts_in_the_flow():
    """A stub that passes between two neighbouring pads uses that pad-pair cut: the flow sends at most
    ``tracks`` escapes through it (design §4.5: nodes = pads, pad-pair cuts, via columns)."""
    def job(name, opts):
        return SimpleNamespace(key=(name, 0.0, 0.0), options=opts)

    def opt(x, cost, cut=None):
        o = {"via": (x, 0.0), "cost": cost, "seq": 0}
        if cut:
            o["cut"] = cut
        return o
    one = ("U1.1", "U1.2", 1)
    a = job("A", [opt(0.0, 1.0, one), opt(5.0, 3.0)])
    b = job("B", [opt(1.0, 1.0, one), opt(6.0, 4.0)])
    ch = F._assign(None, [a, b], set(), 0.5)
    assert (ch[a.key]["cost"], ch[b.key]["cost"]) == (3.0, 1.0)            # one through the gap, the cheaper detour
    two = ("U1.1", "U1.2", 2)
    a = job("A", [opt(0.0, 1.0, two), opt(5.0, 3.0)])
    b = job("B", [opt(1.0, 1.0, two), opt(6.0, 4.0)])
    ch = F._assign(None, [a, b], set(), 0.5)
    assert (ch[a.key]["cost"], ch[b.key]["cost"]) == (1.0, 1.0)
    # a via column that is taken is not offered again; two pads never share a column
    ch = F._assign(None, [a, b], {(0, 0)}, 0.5)
    assert (ch[a.key]["cost"], ch[b.key]["cost"]) == (3.0, 1.0)
    c = job("C", [opt(0.1, 1.0), opt(9.0, 2.0)])
    d = job("D", [opt(0.2, 1.5)])
    ch = F._assign(None, [c, d], set(), 0.5)
    assert (ch[c.key]["cost"], ch[d.key]["cost"]) == (2.0, 1.5)            # D has no other column: C gives way
    # geometry: two staggered rows on one side of a part; the inner row's stubs towards the outer row pass
    # between its pads and are booked on that cut
    pads = [P("J1", i + 1, f"A{i + 1}", 20.0 + i * 1.0, 30.0, 0.15, 0.4) for i in range(6)]            # outer row
    pads += [P("J1", 10 + i, f"B{i + 1}", 20.5 + i * 1.0, 31.6, 0.15, 0.4) for i in range(5)]          # inner row
    pads += [P("J1", 30 + i, "", 19.0 + i * 7.0, 36.0, 0.5, 0.5) for i in range(2)]                    # mounting pads
    pairs = []
    for i in range(5):
        pads.append(tp(f"TP{i + 1}", f"B{i + 1}", 18.0 + i * 3.0, 15.0, kind="smd", layer=1))
        pairs.append((f"J1.{10 + i}", f"TP{i + 1}.1"))
    b_ = mkboard(pads, rules={"default": dict(RULES["default"], via=0.5, drill=0.2)})
    ctx = mkctx(b_, conns_for(b_, pairs))
    rows = F.footprint_rows(b_, "J1")
    inner = next(r for r in rows if r.pads[0].num == "10")
    outer = next(r for r in rows if r.pads[0].num == "1")
    assert (round(outer.n[1]), round(inner.n[1])) == (-1, 1)        # the inner row's free side is away from the outer row
    jobs = F._jobs(ctx)
    j = next(v for v in jobs.values() if v.pad.name == "J1.12")
    seg = {"segs": [(j.pad.x, j.pad.y, j.pad.x, 28.0)]}
    assert F._crossed_cut(ctx, j, seg) == ("J1.3", "J1.4")
    assert F._crossed_cut(ctx, j, {"segs": [(j.pad.x, j.pad.y, j.pad.x, 33.0)]}) is None
    ctx.x.fanout_parts = {"J1": {"style": "in", "side": None, "layer": None}}      # 'in' = towards the outer row here
    res = F.via_escape(ctx, "J1", [f"J1.{10 + i}" for i in range(5)])
    assert all(r["ok"] for r in res), res
    assert verify(ctx) == [] and all(r["terminal"].y < 29.6 for r in res)        # the vias stand beyond the outer row
    recs = [F.escape_of(ctx, f"J1.{10 + i}") for i in range(5)]
    assert all(r["kind"] == "via" and r["desc"]["dir"] == "in" for r in recs)


def test_bga_gets_dog_bones():
    small_via = {"default": dict(RULES["default"], via=0.5, drill=0.25)}
    pads = bga("U2", 30, 30, 4, 4, 1.0, 0.45)
    pairs = []
    for k in (1, 2, 6, 7, 11, 16):
        pads.append(tp(f"TP{k}", f"B{k}", 10.0 + k * 2.5, 50.0, kind="smd", layer=1))
        pairs.append((f"U2.{k}", f"TP{k}.1"))
    b = mkboard(pads, rules=small_via)
    ctx = mkctx(b, conns_for(b, pairs))
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.data["parts"]["U2"]["via"] == 6 and rep.routed == 6, (rep.data, rep.notes)
    assert verify(ctx) == []
    for e in rep.data["escapes"]:
        assert e["kind"] == "via" and e["desc"]["dir"] == "dogbone"
        pad = next(p for p in b.pads if p.name == e["pad"])
        assert abs(abs(e["end"][0] - pad.x) - 0.5) < 1e-9 and abs(abs(e["end"][1] - pad.y) - 0.5) < 1e-9
    ends = [tuple(e["end"]) for e in rep.data["escapes"]]
    assert len(set(ends)) == 6                                                  # pads 1, 2, 6, 7 share sites: one via each
    # a BGA too dense for its via (0.5 mm, 0.3 mm balls, 0.2 / 0.2 rules: not even a track leaves an inner ball)
    # is reported for the critique, and the inner ball as boxed in
    pads = bga("U3", 30, 30, 4, 4, 0.5, 0.3)
    pads.append(tp("TP1", "B6", 10.0, 50.0, kind="smd", layer=1))
    b = mkboard(pads)
    ctx = mkctx(b, conns_for(b, [("U3.6", "TP1.1")]))
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.routed == 0 and ctx.state.pieces() == [] and rep.failed == 1
    assert [c["kind"] for c in rep.data["critique"]] == ["bga_escape"] and rep.data["critique"][0]["ref"] == "U3"
    assert rep.data["boxed"][0]["pad"] == "U3.6" and rep.data["boxed"][0]["cut"].capacity_mm == 0.0


# ── boxed in ──────────────────────────────────────────────────────────────
def boxed_ctx(state=MiniState, walls="wnse"):
    """SOIC-8; pad 2 (west column) is walled in on F.Cu by tracks of net X: in front of the column (w), between
    it and its neighbours (n, s) and behind it (e). Every gap pad-to-wall is 0.235 .. 0.25 mm: legal copper."""
    pads = dual("U1", 30, 30, 4, 1.27, 0.6, 1.5, 5.4)
    pairs = []
    for k in range(1, 9):
        x = 15.0 if k <= 4 else 45.0
        pads.append(tp(f"TP{k}", f"S{k}", x, 20.0 + k * 2.0, kind="smd"))
        pairs.append((f"U1.{k}", f"TP{k}.1"))
    y2 = 30 - 1.905 + 1.27                      # pad 2: x 26.55 .. 28.05, y 29.065 .. 29.665
    yn, ys = y2 - 0.635, y2 + 0.635             # the middle of the gaps to pads 1 and 3
    seg = {"w": ("X", 0, 26.2, yn, 26.2, ys, 0.2, 0, False), "n": ("X", 0, 26.2, yn, 28.4, yn, 0.2, 0, False),
           "s": ("X", 0, 26.2, ys, 28.4, ys, 0.2, 0, False), "e": ("X", 0, 28.4, yn, 28.4, ys, 0.2, 0, False)}
    b = mkboard(pads, tracks=[seg[k] for k in walls], nets={"X": {}})
    return mkctx(b, conns_for(b, pairs), state=state)


def test_boxed_pad_is_reported_with_its_cut():
    for st in STATES:
        ctx = boxed_ctx(state=st)
        rep = F.run(ctx, Budget(10_000_000))
        assert rep.failed == 1 and rep.data["parts"]["U1"]["boxed"] == 1, (rep.notes, rep.data["unescaped"])
        box = rep.data["boxed"][0]
        assert box["pad"] == "U1.2" and box["net"] == "S2" and box["reason"] == "pad boxed in"
        assert box["tried"] == ["fanout"] and box["static_share"] == 1.0
        assert box["blocked_by"] == ["X"] and box["blockers"] == ["track"]
        cut = box["cut"]
        # the neighbours' gap would carry a track (1.94 mm): what closes the pad is the wall, and the cut says so
        assert isinstance(cut, Cut) and (cut.a, cut.b, cut.layer) == ("U1.2", "track", "F.Cu")
        assert cut.nets == ["S2", "X"] and cut.demand_mm == 0.4 and cut.capacity_mm == 0.0
        assert rep.data["cuts"] == [cut]
        # its connection is failed at once, with the certificate; nobody else is touched
        c = ctx.conns[1]
        assert ctx.state.status(c.idx) == "failed" and not c.a.escape
        fail = ctx.state.fail(c.idx) if hasattr(ctx.state, "fail") else ctx.state.fails.get(c.idx)
        if fail is not None:
            assert fail["reason"] == "pad boxed in" and fail["cut"] is cut
            assert fail["probe"] == {"enclosed": True, "end": "a"}
            assert fail["where"] == [27.3, round(30 - 1.905 + 1.27, 4)]
        assert [ctx.state.status(k.idx) for k in ctx.conns].count("failed") == 1
        assert [(f["net"], f["from"], f["to"], f["conn"]) for f in rep.data["failed"]] == [("S2", "U1.2", "TP2.1", 1)]
        assert any("boxed in" in n and "U1.2" in n for n in rep.notes)
        assert rep.data["parts"]["U1"]["escaped"] == 7 and verify(ctx) == []
        assert F.registry(ctx)["boxed"][("U1.2", 27.3, round(30 - 1.905 + 1.27, 4))]["pad"] == "U1.2"


def test_fine_pitch_too_tight_for_the_class_is_boxed_by_its_neighbours():
    """0.2 mm pads at 0.4 mm with a 0.25 mm / 0.2 mm class and no narrower neck: a track of that width cannot
    even sit on its pad without coming within 0.175 mm of the neighbours. The certificate names the pad-pair
    cut between the two neighbours (capacity 0)."""
    ctx = dense_ctx(pad_w=0.2)
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.failed == 10 and rep.routed == 0 and ctx.state.pieces() == []
    by = {b["pad"]: b for b in rep.data["boxed"]}
    cut = by["J1.5"]["cut"]
    assert (cut.a, cut.b, cut.layer, cut.capacity_mm, cut.demand_mm, cut.nets) == ("J1.4", "J1.6", "F.Cu", 0.0, 0.45, ["N5"])
    assert set(by["J1.5"]["blockers"]) == {"J1.4", "J1.6"} and set(by["J1.5"]["blocked_by"]) == {"N4", "N6"}
    assert by["J1.5"]["static_share"] == 1.0
    end = by["J1.1"]["cut"]                                     # an end pad has one neighbour: pad against blocker
    assert (end.a, end.b, end.capacity_mm) == ("J1.1", "J1.2", 0.0)
    assert all(ctx.state.status(c.idx) == "failed" for c in ctx.conns)
    # the same row with a neck the pads allow: nothing is boxed
    ctx = dense_ctx(pad_w=0.2)
    for c in ctx.conns:
        c.width, c.neck = 0.25, 0.2
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.failed == 0 and rep.routed == 10 and verify(ctx) == []
    necked = [p for p in ctx.state.pieces() if p.kind == "seg"]
    assert necked and all(p.w == 0.2 and p.target_w == 0.25 for p in necked)
    assert all(e["necked"] for e in rep.data["escapes"])


def test_a_pad_with_any_exit_is_not_called_boxed():
    """The strict test: with walls in front and behind (and vias too big to fit anywhere) the analytic escapes
    all fail, but the pad can still leave diagonally past the wall ends, so it is left to the detail stage and
    its connection stays 'todo'."""
    pads = dual("U1", 30, 30, 4, 1.27, 0.6, 1.5, 5.4)
    pads.append(tp("TP2", "S2", 15.0, 24.0, kind="smd"))
    y2 = 30 - 1.905 + 1.27
    walls = [("X", 0, 26.2, y2 - 0.1, 26.2, y2 + 0.1, 0.2, 0, False), ("X", 0, 28.4, y2 - 0.1, 28.4, y2 + 0.1, 0.2, 0, False)]
    b = mkboard(pads, tracks=walls, nets={"X": {}}, rules={"default": dict(RULES["default"], via=2.4, drill=1.2)})
    ctx = mkctx(b, conns_for(b, [("U1.2", "TP2.1")]))
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.failed == 0 and rep.data["boxed"] == [] and rep.routed == 0
    assert [u["pad"] for u in rep.data["unescaped"]] == ["U1.2"], rep.data
    assert rep.data["parts"]["U1"] == {"escaped": 0, "ring": 0, "via": 0, "unescaped": 1, "boxed": 0}
    assert ctx.state.status(0) == "todo" and not ctx.conns[0].a.escape and ctx.state.pieces() == []


# ── book-keeping: terminals, sync, undo, re_escape ────────────────────────
def test_sync_and_undo_follow_the_state():
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, span=4.0, ep=2.0)
    orig = {c.idx: c.a for c in ctx.conns}
    F.run(ctx, Budget(10_000_000))
    assert all(c.a.escape for c in ctx.conns) and F.sync(ctx) == 0
    # somebody removes the stub of U1.3 (a judge blaming it, a snapshot restore)
    rec = F.escape_of(ctx, "U1.3")
    for pid in rec["pieces"]:
        ctx.state.remove(pid)
    assert F.sync(ctx) == 1
    c3 = next(c for c in ctx.conns if orig[c.idx].pad == "U1.3")
    assert c3.a is orig[c3.idx] and not c3.a.escape and F.escape_of(ctx, "U1.3") is None
    assert sum(1 for c in ctx.conns if c.a.escape) == 15
    # the pad can be escaped again
    res = F.ring_fan(ctx, "U1", ["U1.3"])
    assert res[0]["ok"] and c3.a.escape and verify(ctx) == []
    n = F.undo(ctx)
    assert n == 16 and ctx.state.pieces() == [] and all(c.a is orig[c.idx] for c in ctx.conns)
    assert F.registry(ctx)["by_pad"] == {}


def test_snapshot_restores_keep_terminals_and_stubs_together():
    """The engine's keep-best gate and the judge restore snapshots without knowing about escape terminals:
    run() hooks state.restore (this instance) so the book follows."""
    rs = real_state()
    if rs is None:
        print("    (skipped: pieces.RouteState is still a stub)")
        return
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, span=5.0, ep=0.0, state=rs)
    orig = {c.idx: c.a for c in ctx.conns}
    empty = ctx.state.snapshot()
    F.run(ctx, Budget(10_000_000))
    fanned = ctx.state.snapshot()
    esc = {c.idx: c.a for c in ctx.conns}
    c = ctx.conns[0]
    assert F.re_escape(ctx, c, "a", "") and c.a is not esc[c.idx]
    moved = ctx.state.snapshot()
    moved_term = c.a
    # back to before the re-escape: the old stub is back and the terminal points at its end again
    ctx.state.restore(fanned)
    assert c.a is esc[c.idx] and F.escape_of(ctx, c.a.pad)["terminal"] is c.a
    assert all(ctx.oracle.has_piece(i) for i in F.escape_of(ctx, c.a.pad)["pieces"])
    # forward again, then all the way back to the empty board: every connection starts on its pad
    ctx.state.restore(moved)
    assert c.a is moved_term
    ctx.state.restore(empty)
    assert ctx.state.pieces() == [] and all(k.a is orig[k.idx] for k in ctx.conns)
    assert F.registry(ctx)["by_pad"] == {}
    # and forward to the fanned-out board: the escapes are taken up again
    ctx.state.restore(fanned)
    assert all(k.a is esc[k.idx] for k in ctx.conns) and len(F.registry(ctx)["by_pad"]) == 16
    assert verify(ctx) == []
    # a stage that dies half-way leaves no escape terminal without its stub
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, span=5.0, ep=0.0, state=rs)
    calls = [0]
    real_add = ctx.state.add

    def flaky(**f):
        calls[0] += 1
        if calls[0] == 6:
            raise RuntimeError("boom")
        return real_add(**f)
    ctx.state.add = flaky
    try:
        F.run(ctx, Budget(10_000_000))
        raise AssertionError("the injected fault must propagate")
    except RuntimeError:
        pass
    assert not any(k.a.escape for k in ctx.conns) and F.registry(ctx)["by_pad"] == {}
    assert [p for p in ctx.state.pieces() if p.stage == "fanout"] == []


def test_re_escape_moves_the_stub():
    for st in STATES:
        ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, span=5.0, ep=0.0, state=st)
        F.run(ctx, Budget(10_000_000))
        c = ctx.conns[0]                                        # U1.1: first pad of the west row
        before = c.a
        rec0 = F.escape_of(ctx, "U1.1")
        n0 = len(ctx.state.pieces())
        assert F.re_escape(ctx, c, "a", "") is True
        rec1 = F.escape_of(ctx, "U1.1")
        assert rec1 is not rec0 and rec1["desc"] != rec0["desc"] and c.a is rec1["terminal"] and c.a.escape
        assert (c.a.x, c.a.y) != (before.x, before.y)
        ids = {p.id for p in ctx.state.pieces()}
        assert not (set(rec0["pieces"]) & ids) and set(rec1["pieces"]) <= ids
        assert verify(ctx) == [] and crossings(ctx) == []
        # avoiding the side the pad's row faces (west) leaves only an inward via
        assert F.re_escape(ctx, c, "a", "w") is True
        rec2 = F.escape_of(ctx, "U1.1")
        assert rec2["kind"] == "via" and rec2["desc"]["dir"] == "in" and c.a.x > 27.5 and c.a.layers == (0, 1)
        assert verify(ctx) == []
        # a rollback of the caller's transaction is healed by sync(): back to the newest escape that still exists
        tok = ctx.state.begin()
        assert F.re_escape(ctx, c, "a", "in") is True and F.escape_of(ctx, "U1.1")["kind"] == "ring"
        ctx.state.rollback(tok)
        if F.sync(ctx):
            pass
        assert F.escape_of(ctx, "U1.1") is None or F.escape_of(ctx, "U1.1")["kind"] == "via"
        if F.escape_of(ctx, "U1.1") is None:
            assert not c.a.escape
        # refused while another connection is routed onto the same stub
        c2 = ctx.conns[1]
        extra = ConnV2(idx=99, net=c2.net, a=c2.a, b=c2.b, rank=(99,))
        ctx.conns.append(extra)
        F.escape_of(ctx, c2.a.pad)["orig"][(99, "a")] = F.escape_of(ctx, c2.a.pad)["orig"][(c2.idx, "a")]
        ctx.state.set_status(99, "routed")
        assert F.re_escape(ctx, c2, "a", "") is False
        assert len(ctx.state.pieces()) >= n0 - 1
        try:
            F.re_escape(ctx, c2, "x", "")
            raise AssertionError("end must be 'a' or 'b'")
        except ValueError:
            pass


def test_re_escape_of_a_pad_that_never_had_one():
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, span=4.0, ep=2.0, fanout="none")
    assert F.run(ctx, Budget(10_000_000)).routed == 0 and ctx.state.pieces() == []
    c = ctx.conns[5]
    assert F.re_escape(ctx, c, "a", "") is True and c.a.escape and verify(ctx) == []
    assert F.re_escape(ctx, c, "b", "") is False               # a through-hole test point needs no escape


# ── what gets no escape ───────────────────────────────────────────────────
def test_plane_short_and_copper_pads_are_left_alone():
    pads = quad("U1", 30.0, 30.0, 6, 0.5, 0.25, 0.8, 5.0)
    pads += [tp("TP1", "N1", 12.0, 28.0), tp("TP2", "N2", 12.0, 30.0), tp("TP4", "N4", 12.0, 34.0),
             P("C1", 1, "N3", 26.1, 29.75, 0.25, 0.3), P("C1", 2, "GNDX", 25.1, 29.75, 0.25, 0.3),
             tp("TP5", "N5", 12.0, 36.0)]
    b = mkboard(pads)
    conns = conns_for(b, [("U1.1", "TP1.1"), ("U1.2", "TP2.1"), ("U1.3", "C1.1"), ("U1.4", "TP4.1"), ("U1.5", "TP5.1")])
    conns[1].role = "plane"                                     # S1 owns plane pads
    ctx = mkctx(b, conns)
    pad5 = next(p for p in b.pads if p.name == "U1.5")
    ctx.state.add(net="N5", conn=-1, kind="seg", layer=0, x1=pad5.x, y1=pad5.y, x2=pad5.x - 1.0, y2=pad5.y, w=0.2,
                  stage="power", protected=True)                # a stub somebody else already placed
    ctx.state.set_status(3, "routed")                           # U1.4's connection is done
    rep = F.run(ctx, Budget(10_000_000))
    assert [e["pad"] for e in rep.data["escapes"]] == ["U1.1"], rep.data["escapes"]
    assert conns[0].a.escape and not any(c.a.escape for c in conns[1:])
    assert verify(ctx) == []
    res = F.ring_fan(ctx, "U1", ["U1.1", "U1.2", "U1.3", "U1.5", "U1.6"])
    assert [r.get("skip") for r in res] == ["pad already has an escape" if False else res[0].get("skip"),
                                           "no connection to route from this pad", "partner next to the pad",
                                           "pad already has copper", "no connection to route from this pad"]


def test_through_hole_parts_are_not_fanned_unless_asked():
    pads = [P("J1", i + 1, f"N{i + 1}", 20.0 + (i % 10) * 2.54, 30.0 + (i // 10) * 2.54, 0.85, 0.85, r=0.85,
              kind="tht", hole=0.5) for i in range(20)]
    pairs = []
    for i in range(20):
        pads.append(tp(f"TP{i + 1}", f"N{i + 1}", 8.0 + i * 2.2, 10.0 if i < 10 else 50.0))
        pairs.append((f"J1.{i + 1}", f"TP{i + 1}.1"))
    b = mkboard(pads)
    assert F.needs_fanout(b, "J1", 0.2, 0.2)                    # >= 16 pads on rows
    ctx = mkctx(b, conns_for(b, pairs))
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.routed == 0 and ctx.state.pieces() == [] and rep.data["parts"] == {}
    ctx = mkctx(b, conns_for(b, pairs), fanout_parts={"J1": {"style": "out", "side": None, "layer": 1}})
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.routed == 20 and verify(ctx) == []
    assert all(p.layer == 1 for p in ctx.state.pieces()) and all(c.a.layers == (1,) for c in ctx.conns)


# ── plan styles ───────────────────────────────────────────────────────────
def test_plan_styles():
    # none (global and per part)
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, ep=2.0, fanout="none")
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.routed == 0 and any("off" in n for n in rep.notes)
    # a per-part entry wins over the global switch
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, ep=2.0, fanout="none",
                  fanout_parts={"U1": {"style": "out", "side": None, "layer": None}})
    assert F.run(ctx, Budget(10_000_000)).routed == 16
    # via: every pad to a via, although the partners are reachable on the pad layer
    ctx = qfn_ctx(n=4, pitch=0.65, pw=0.3, ep=1.2, span=5.0, fanout_parts={"U1": {"style": "via", "side": None, "layer": None}})
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.data["parts"]["U1"]["ring"] == 0 and rep.data["parts"]["U1"]["via"] >= 12 and verify(ctx) == []
    # in: only inward vias
    ctx = qfn_ctx(n=4, pitch=0.65, pw=0.3, ep=0.0, span=6.0, fanout_parts={"U1": {"style": "in", "side": None, "layer": None}})
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.data["parts"]["U1"]["via"] >= 8 and verify(ctx) == []
    assert all(e["desc"]["dir"] == "in" for e in rep.data["escapes"])
    for e in rep.data["escapes"]:
        assert math.hypot(e["end"][0] - 30.0, e["end"][1] - 30.0) < 3.0 * math.sqrt(2)
    # out: ring only; a pad the ring cannot serve is not sent to a via
    ctx = walled_ctx(fanout_parts={"J1": {"style": "out", "side": None, "layer": None}})
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.data["parts"]["J1"] == {"escaped": 6, "ring": 6, "via": 0, "unescaped": 4, "boxed": 0} and verify(ctx) == []
    # layer: the escapes must end on B.Cu -> vias, terminals on that layer
    ctx = qfn_ctx(n=4, pitch=0.65, pw=0.3, ep=1.2, span=5.0, fanout_parts={"U1": {"style": "auto", "side": None, "layer": 1}})
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.data["parts"]["U1"]["ring"] == 0 and rep.data["parts"]["U1"]["via"] >= 12
    assert all(c.a.layers == (1,) for c in ctx.conns if c.a.escape)
    # side: a single-row part has no outward side; the plan (or the partners) pick it
    for side, sign in (("n", -1), ("s", 1)):
        pads = [P("J1", i + 1, f"N{i + 1}", 25.0 + i * 1.0, 30.0, 0.3, 0.6) for i in range(6)]
        pairs = []
        for i in range(6):
            pads.append(tp(f"TP{i + 1}", f"N{i + 1}", 25.0 + i * 1.0, 45.0))            # partners to the south
            pairs.append((f"J1.{i + 1}", f"TP{i + 1}.1"))
        b = mkboard(pads)
        ctx = mkctx(b, conns_for(b, pairs), fanout_parts={"J1": {"style": "out", "side": side, "layer": None}})
        rep = F.run(ctx, Budget(10_000_000))
        assert rep.routed == 6 and all((c.a.y - 30.0) * sign > 0 for c in ctx.conns), side
    ctx = mkctx(b, conns_for(b, pairs))
    assert F.run(ctx, Budget(10_000_000)).routed == 6 and all(c.a.y > 30.0 for c in ctx.conns)   # towards the partners


def test_reserved_sites_and_thermal_spokes():
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, span=4.0, ep=2.0)
    pad = next(p for p in ctx.board.pads if p.name == "U1.2")
    # a via site reserved for another net right where U1.2's straight stub would end: no escape goes through
    # it; the pad is left to the detail stage (reported, not boxed)
    sid = ctx.oracle.add_soft("reserve", "N9", -1, geom.box_circle(pad.x - 0.8, pad.y, 0.15))
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.routed == 15 and rep.failed == 0 and verify(ctx) == []
    assert F.escape_of(ctx, "U1.2") is None and [u["pad"] for u in rep.data["unescaped"]] == ["U1.2"]
    assert rep.data["unescaped"][0]["blocked_by"] == [f"soft:{sid}"]
    for p in ctx.state.pieces():
        assert [h for h in ctx.oracle.soft_hits(max(p.layer, 0), p.x1, p.y1, p.x2, p.y2, p.w, p.net)
                if h.rule == "reserve"] == []
    # the owner of the reservation may use it
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, span=4.0, ep=2.0)
    ctx.oracle.add_soft("reserve", "N2", -1, geom.box_circle(pad.x - 0.8, pad.y, 0.15))
    assert F.run(ctx, Budget(10_000_000)).routed == 16
    assert F.escape_of(ctx, "U1.2")["desc"] == {"ring": 1, "from": 1, "dir": 0}
    # a thermal spoke is priced, not forbidden: with no other way the stub crosses it
    ctx = qfn_ctx(n=4, pitch=0.5, pw=0.25, span=4.0, ep=2.0)
    ctx.oracle.add_soft("thermal", "GND", 0, (pad.x - 1.5, 30.0, 1.0, 0.0, 2.5, 2.5, 0.0))     # covers the whole west side
    rep = F.run(ctx, Budget(10_000_000))
    assert rep.routed == 16 and F.escape_of(ctx, "U1.2")["soft"] >= 1


# ── determinism and budget ────────────────────────────────────────────────
def _dump(ctx):
    return [(p.id, p.net, p.kind, p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.drill) for p in ctx.state.pieces()], \
        [(c.idx, c.a.x, c.a.y, c.a.layers, c.a.escape) for c in ctx.conns]


def test_same_twice():
    for make in (qfn_ctx, qfp_fan_ctx, dense_ctx, walled_ctx, lambda: qfn_ctx(rot=45.0), lambda: qfn_ctx(n=6, pitch=0.5, pw=0.25, span=5.0, ep=2.6, partner="smd")):
        a, b = make(), make()
        ra, rb = F.run(a, Budget(10_000_000)), F.run(b, Budget(10_000_000))
        assert _dump(a) == _dump(b) and ra.work == rb.work and ra.notes == rb.notes
        assert json.dumps(ra.data["escapes"], sort_keys=True) == json.dumps(rb.data["escapes"], sort_keys=True)
    # the order of ctx.conns does not matter (jobs are taken in rank order)
    a, b = qfp_fan_ctx(), qfp_fan_ctx()
    b.conns.reverse()
    F.run(a, Budget(10_000_000))
    F.run(b, Budget(10_000_000))
    assert _dump(a)[0] == _dump(b)[0]


def test_budget_is_charged_and_respected():
    ctx = qfn_ctx()
    bud = Budget(10_000_000)
    rep = F.run(ctx, bud)
    assert bud.used == rep.work > 32 * 200
    # a spent budget: nothing is placed, and that is said
    ctx = qfn_ctx()
    bud = Budget(0)
    rep = F.run(ctx, bud)
    assert rep.routed == 0 and ctx.state.pieces() == [] and any("budget" in n for n in rep.notes)
    # two parts, budget for one: the first (by reference) is fanned out, the second is left
    pads = quad("U1", 15.0, 30.0, 4, 0.5, 0.25, 0.8, 4.0) + quad("U2", 45.0, 30.0, 4, 0.5, 0.25, 0.8, 4.0, net=lambda i: f"M{i}")
    pairs = []
    for k in range(1, 17):
        pads += [tp(f"TA{k}", f"N{k}", 3.0 + k * 3.2, 5.0), tp(f"TB{k}", f"M{k}", 3.0 + k * 3.2, 55.0)]
        pairs += [(f"U1.{k}", f"TA{k}.1"), (f"U2.{k}", f"TB{k}.1")]
    b = mkboard(pads)
    ctx = mkctx(b, conns_for(b, pairs))
    rep = F.run(ctx, Budget(1000))                              # 200 per pad: enough for five pads, checked per row
    assert set(rep.data["parts"]) == {"U1"} and rep.routed == 8 and any("budget" in n for n in rep.notes)
    assert verify(ctx) == [] and not any(c.a.escape for c in ctx.conns if c.net.startswith("M"))


def test_throughput():
    """Measured (not a gate): pads per second and oracle checks per pad of the ring fan and of the via escape
    (flow + oracle), the stage call alone."""
    def measure(make, n=5):
        ctxs = [make() for _ in range(n)]
        t = time.perf_counter()
        pads = sum(F.run(c, Budget(10_000_000)).routed for c in ctxs)
        dt = time.perf_counter() - t
        return pads / dt, sum(c.oracle.checks for c in ctxs) / max(1, pads)
    ring, rc = measure(qfp_fan_ctx)
    via, vc = measure(lambda: qfn_ctx(n=6, pitch=0.5, pw=0.25, pl=0.8, span=7.0, ep=0.0, partner="smd"))
    print(f"    ring fan: {ring:.0f} pads/s ({rc:.0f} oracle checks per pad)   via escape: {via:.0f} pads/s "
          f"({vc:.0f} checks per pad)")
    assert ring > 100 and via > 30


# ── real boards, judged by KiCad ──────────────────────────────────────────
def have_kicad() -> bool:
    return common.KICAD_CLI.exists() and common.KICAD_PY.exists()


def corpus_ctx(board: str, tag="fanout"):
    """(ctx, scratch pcb path) for a corpus board: connections = one per pad to its nearest same-net pad
    already in the tree (Prim), plane role for nets with a pour."""
    from copper_kicad.pcb.guided import session
    d = common.WORK / "w6" / tag / board
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    for f in (common.CORPUS / board).iterdir():
        if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
            shutil.copy2(f, d / f.name)
    pcb = d / f"{board}.kicad_pcb"
    ex = session._run(pcb, [{"op": "extract"}], save=False)[0]
    b = model.Board(ex, model.dru_minimums(pcb.with_suffix(".kicad_dru")))
    conns = []
    dfw = max(b.rules["default"].get("width") or 0.25, b.rules["track_width"])     # classify: neck = min(class, Default)
    for name in sorted(b.nets):
        net = b.nets[name]
        pads = [p for p in net.pads if p.layers and p.shapes]
        if len(pads) < 2:
            continue
        tree, rest = [pads[0]], pads[1:]
        while rest:
            best = None
            for p in rest:
                for t in tree:
                    dd = (round(math.hypot(p.x - t.x, p.y - t.y), 6), p.name, t.name, p.x, p.y)
                    if best is None or dd < best[0]:
                        best = (dd, p, t)
            _dd, p, t = best
            rest.remove(p)
            tree.append(p)
            conns.append(ConnV2(idx=len(conns), net=name,
                                a=Terminal(p.x, p.y, tuple(p.layers), pad=p.name, cluster=p.cluster),
                                b=Terminal(t.x, t.y, tuple(t.layers), pad=t.name, cluster=t.cluster),
                                est=_dd[0], role="plane" if net.has_pour else "signal", width=net.width,
                                neck=min(net.width, dfw), clearance=net.clearance, via=(net.via, net.drill),
                                rank=(len(conns),)))
    orc = O.Oracle(b, overrides=O.local_overrides_from_file(pcb))
    ctx = Ctx(board=b, cfg=None, x=SimpleNamespace(fanout="auto", fanout_parts={}), oracle=orc,
              state=MiniState(orc, conns), conns=conns)
    return ctx, pcb


def kicad_judges(ctx, pcb) -> dict:
    """Commit the fan-out copper to the scratch board and ask kicad-cli: which error-severity violations name
    one of our items (dangling ends excepted: an escape is a stub until the detail stage arrives)?"""
    import oracle_diff
    from copper_kicad.pcb.guided import session
    names = ctx.board.layers
    pcs = ctx.state.pieces()
    tracks = [[p.net, names[p.layer], p.x1, p.y1, p.x2, p.y2, p.w] for p in pcs if p.kind == "seg"]
    vias = [[p.net, p.x1, p.y1, p.w, p.drill] for p in pcs if p.kind == "via"]
    base = oracle_diff.kicad_drc(pcb)
    cm = session._run(pcb, [{"op": "commit", "tracks": tracks, "vias": vias}], save=True)[0]
    ours = set(cm["track_uuids"]) | set(cm["via_uuids"])
    assert len(ours) == len(tracks) + len(vias)
    raw = oracle_diff.kicad_drc(pcb)
    mine = []
    for v in raw:
        if v.get("severity") != "error" or v.get("type") in ("track_dangling", "via_dangling", "unconnected_items"):
            continue
        if any(i.get("uuid") in ours for i in v.get("items", [])):
            mine.append((v.get("type"), v.get("description"), [i.get("description") for i in v.get("items", [])]))
    nbase = sum(1 for v in base if v.get("severity") == "error")
    nraw = sum(1 for v in raw if v.get("severity") == "error" and v.get("type") not in ("track_dangling", "via_dangling"))
    return {"tracks": len(tracks), "vias": len(vias), "on_ours": mine, "errors_before": nbase, "errors_after": nraw}


def test_corpus_escapes_pass_kicad_drc():
    """The gate behind 'analytic escapes checked only by the oracle': on real boards KiCad's own DRC finds no
    error on any escape stub or via."""
    if not have_kicad():
        print("    (skipped: KiCad not found)")
        return
    boards = ["synth_qfp_fanout", "tinytapeout"]
    if FULL:
        boards = sorted(p.name for p in common.CORPUS.iterdir() if (p / "meta.json").exists())
    boards = [b for b in boards if (common.CORPUS / b / f"{b}.kicad_pcb").exists()]
    if not boards:
        print("    (skipped: no benchmark corpus)")
        return
    total = 0
    failures = []
    gaps = []
    for name in boards:
        ctx, pcb = corpus_ctx(name)
        t = time.perf_counter()
        rep = F.run(ctx, Budget(50_000_000))
        dt = time.perf_counter() - t
        tot = {k: sum(s[k] for s in rep.data["parts"].values()) for k in ("escaped", "ring", "via", "unescaped", "boxed")}
        bad = verify_with(ctx, pcb)
        assert bad == [], (name, bad[:3])
        assert crossings(ctx) == [], name
        if not ctx.state.pieces():
            print(f"    {name}: nothing to fan out ({len(ctx.conns)} connections)")
            continue
        k = kicad_judges(ctx, pcb)
        total += tot["escaped"]
        print(f"    {name}: {len(rep.data['parts'])} parts, {tot}, {dt:.2f} s, work {rep.work}; KiCad: {k['tracks']} "
              f"tracks + {k['vias']} vias, errors on them: {len(k['on_ours'])} (board errors {k['errors_before']} -> "
              f"{k['errors_after']})")
        # a rule the oracle does not model (W0's list, plus what this test found: docs/internal/v2-interface-notes.md W6):
        # the clearance override of an NPTH pad applied to its hole. Counted, printed, not a fan-out fault.
        known = [e for e in k["on_ours"] if e[0] == "hole_clearance" and "pad clearance" in (e[1] or "")
                 and any("NPTH pad" in (i or "") for i in e[2])]
        other = [e for e in k["on_ours"] if e not in known]
        gaps += [(name, e) for e in known]
        if known:
            print(f"      oracle gap (NPTH pad clearance override on the hole): {len(known)} item(s)")
        if other:
            failures.append((name, other[:5]))
        if k["errors_after"] - len(known) > k["errors_before"]:
            failures.append((name, "new board errors", k["errors_before"], k["errors_after"]))
        if name == "synth_qfp_fanout":
            assert tot["escaped"] >= 56 and tot["boxed"] == 0, tot
        if name == "tinytapeout":
            assert tot["escaped"] >= 100 and tot["boxed"] == 0, tot
    assert failures == [], failures
    assert len(gaps) <= 2, gaps
    assert total > 0


def verify_with(ctx, pcb):
    o = O.Oracle(ctx.board, overrides=O.local_overrides_from_file(pcb))
    bad = []
    for p in ctx.state.pieces():
        b = o.piece_ok(p)
        if b is not None:
            bad.append((p, b))
        o.add_piece(p)
    return bad


if __name__ == "__main__":
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t0 = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t0:.1f}s)")
            except Exception as ex:  # noqa: BLE001
                fails += 1
                traceback.print_exc()
                print(f"FAIL {name}: {ex}")
    sys.exit(1 if fails else 0)
