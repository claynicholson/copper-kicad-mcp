"""Deterministic rip-up-and-reroute engine.

Input: a ``model.Board`` and a resolved plan (``plan.Config``). Output: the
tracks and vias to add plus an honest per-connection report. No randomness,
no clocks in any decision except the overall time budget (which only ever
turns remaining work into reported failures).

Pipeline per connection (a ratsnest edge between two pad clusters of a net):
  1. sources / targets = grid cells of the two copper components (pad centres,
     kept tracks, vias, pour islands, already-routed same-net paths) plus exact
     off-grid *escape stubs* for fine-pitch pads;
  2. A* on the net's occupancy class (width + clearance aware);
  3. if blocked: soft A* through other routed nets, rip those up, bump the
     history cost where the conflict was, re-queue the victims;
  4. turn the cell path into 45° segments, snap ends to pad centres when the
     exact checker allows it, stamp the copper into every occupancy class.
"""
from __future__ import annotations

import math
import time
from array import array
from heapq import heappop, heappush

from . import astar, geom
from .geom import Shape, ShapeIndex
from .grid import BLOCK, EPS

# The time budget is spent in *work units*, not seconds: one A* expansion = 1 unit, one search = SEARCH_WORK
# units of fixed overhead. plan.global.time_budget_s is converted with WORK_PER_SECOND (roughly what a desktop
# CPU does), so the same plan on the same board stops at the same point on a fast and on a slow machine.
WORK_PER_SECOND = 200_000
SEARCH_WORK = 4_000

LARGE_PAD = 2.0      # mm: pads bigger than this may be entered anywhere, smaller ones at their centre


class Conn:
    __slots__ = ("idx", "net", "a", "b", "pa", "pb", "est", "key", "state", "segs", "vias", "marks", "sets",
                 "shapes", "states", "reason", "where", "blocked_by", "rips", "width", "notes", "length", "pour")

    def __init__(self, idx, net, a, b, pa, pb, est, pour=False):
        self.idx, self.net, self.a, self.b, self.pa, self.pb, self.est = idx, net, a, b, pa, pb, est
        self.state = "pending"
        self.segs, self.vias, self.marks, self.sets, self.shapes, self.states = [], [], {}, {}, [], ()
        self.reason = self.where = None
        self.blocked_by = []
        self.rips = 0
        self.width = None
        self.notes = []
        self.length = 0.0
        self.pour = pour

    def label(self):
        return f"{self.pa.name}→{self.pb.name if self.pb else 'pour'}"


class Router:
    def __init__(self, board, cfg, *, mode="signals", progress=None, deadline=None, work=None):
        self.b = board
        self.cfg = cfg
        self.g = board.grid
        self.mode = mode
        self.progress = progress
        self.t0 = time.time()
        # deterministic budget (work units) + a wall-clock safety net far beyond it (slow machine / stuck search)
        self.work_limit = cfg.time_budget * WORK_PER_SECOND if work is None else work
        self.deadline = deadline or (self.t0 + 5 * cfg.time_budget + 60)
        self.clock_hit = False
        self.dyn = ShapeIndex()
        self.routed = []            # conns in commit order
        self.conns = []
        self.hist = None
        self.cluster_cache = {}
        self.comp = {}              # net name -> {cid: comp dict}
        self.cmul_cache = {}
        self.moves_cache = {}
        self.turn = astar.turn_table(cfg.bend_cost / self.g.step)
        self.stats = {"searches": 0, "expansions": 0, "ripups": 0}
        self.skipped = []
        self.notes = []
        self._halo = None
        self.dp_done = {}           # net name -> list of routed polylines (for the partner's corridor)

    def work(self):
        return self.stats["expansions"] + SEARCH_WORK * self.stats["searches"]

    def spent(self):
        """True once the budget is used up."""
        if self.work() >= self.work_limit:
            return True
        if time.time() > self.deadline:
            self.clock_hit = True
            return True
        return False

    # ── planning: which connections exist ─────────────────────────────────
    def plan_connections(self):
        b, cfg = self.b, self.cfg
        idx = 0
        for name in sorted(b.clusters):
            net = b.nets[name]
            cl = b.clusters[name]
            if len(cl) < 2:
                continue
            nc = cfg.net(name)
            if nc.skip:
                self.skipped.append({"net": name, "connections": len(cl) - 1, "reason": nc.skip_reason or "plan: skip"})
                continue
            if net.has_pour != (self.mode == "pours"):
                continue
            cids = sorted(cl)
            if net.has_pour:
                # every cluster but the main one (largest pour island area, then most pads) connects to "anything else"
                def weight(c):
                    return (sum(geom.poly_area(p) for _l, p in cl[c]["islands"]), len(cl[c]["pads"]), -c)
                main = max(cids, key=weight)
                anchors = []
                for c in cids:
                    for p in cl[c]["pads"]:
                        anchors.append((p.x, p.y, c))
                for c in cids:
                    if c == main or not cl[c]["pads"]:
                        continue
                    pa = cl[c]["pads"][0]
                    best = None
                    for p in cl[c]["pads"]:
                        for x, y, c2 in anchors:
                            if c2 != c:
                                d = abs(p.x - x) + abs(p.y - y)
                                if best is None or d < best[0]:
                                    best = (d, p)
                    if best:
                        pa = best[1]
                    self.conns.append(Conn(idx, name, c, None, pa, None, 0.0, pour=True))
                    idx += 1
                continue
            # Prim's MST over clusters (distance = closest pad pair)
            pts = {c: [(p.x, p.y, p) for p in cl[c]["pads"]] for c in cids}
            cids = [c for c in cids if pts[c]]
            if len(cids) < 2:
                continue
            in_tree = [cids[0]]
            rest = cids[1:]
            edges = []
            while rest:
                best = None
                for c in rest:
                    for t in in_tree:
                        for x1, y1, p1 in pts[c]:
                            for x2, y2, p2 in pts[t]:
                                dx, dy = abs(x1 - x2), abs(y1 - y2)
                                d = max(dx, dy) + 0.41421356 * min(dx, dy)
                                if best is None or d < best[0] - 1e-9:
                                    best = (d, c, t, p1, p2)
                d, c, t, p1, p2 = best
                edges.append((c, t, p1, p2, d))
                in_tree.append(c)
                rest.remove(c)
            for c, t, p1, p2, d in edges:
                self.conns.append(Conn(idx, name, c, t, p1, p2, d))
                idx += 1
        # routing order: plan rank, then priority, then short first
        for c in self.conns:
            nc = cfg.net(c.net)
            c.key = (nc.rank, -nc.priority, round(c.est, 3), c.net, c.idx)
        self._pair_order()
        return self.conns

    def _pair_order(self):
        """Diff pairs: the N net is routed right after its P net (same rank / priority / length slot)."""
        cfg = self.cfg
        for c in self.conns:
            nc = cfg.net(c.net)
            if nc.dp_partner and nc.dp_role == "n":
                pc = cfg.net(nc.dp_partner)
                c.key = (pc.rank, -pc.priority, round(c.est, 3), nc.dp_partner + "\x7f", c.idx)

    # ── class maps / marks ────────────────────────────────────────────────
    def cm_for(self, key):
        g = self.g
        new = key not in g.classes
        cm = self.b.class_map(key)
        if new:
            for c in self.routed:
                self._mark(c, cm)
        return cm

    def _mark(self, c, cm):
        g, R = self.g, self.b.rules
        net = self.b.nets[c.net]
        nc = self.cfg.net(c.net)
        cmax = max(nc.clearance, cm.c)
        G = g.G
        t, v, vb = set(), set(), set()
        for lay, x1, y1, x2, y2, w in c.segs:
            base = lay * G
            W = g.W
            for iy, i0, i1 in g.capsule_spans(x1, y1, x2, y2, w / 2 + cmax + cm.h + EPS):
                a = base + iy * W
                t.update(range(a + i0, a + i1 + 1))
            for iy, i0, i1 in g.capsule_spans(x1, y1, x2, y2, w / 2 + cmax + cm.vr + EPS):
                a = iy * W
                v.update(range(a + i0, a + i1 + 1))
        for x, y, size, drill in c.vias:
            rt = max(size / 2 + cmax, drill / 2 + R["hole_clearance"]) + cm.h + EPS
            cells = g.cells(g.capsule_spans(x, y, x, y, rt))
            for l in range(g.L):
                base = l * G
                t.update(i + base for i in cells)
            v.update(g.cells(g.capsule_spans(x, y, x, y, max(size / 2 + cmax, drill / 2 + R["hole_clearance"]) + cm.vr + EPS)))
            vb.update(g.cells(g.capsule_spans(x, y, x, y, drill / 2 + R["hole_to_hole"] + cm.vd / 2 + EPS)))
        nid = net.id
        tl, vl, vbl = sorted(t), sorted(v), sorted(vb)
        cm.mark(False, tl, nid)
        cm.mark(True, vl, nid)
        if vbl:
            cm.mark(True, vbl, BLOCK)
        c.marks[cm.key] = (tl, vl, vbl)
        c.sets[cm.key] = (t, v | vb)

    def _commit(self, c):
        nid = self.b.nets[c.net].id
        nc = self.cfg.net(c.net)
        for lay, x1, y1, x2, y2, w in c.segs:
            s = Shape("box", geom.box_from_seg(x1, y1, x2, y2, w), "cu", nid, nc.clearance, (lay,), tag=c)
            c.shapes.append(s)
            self.dyn.add(s)
        allL = tuple(range(self.g.L))
        for x, y, size, drill in c.vias:
            for s in (Shape("box", geom.box_circle(x, y, size / 2), "cu", nid, nc.clearance, allL, tag=c),
                      Shape("box", geom.box_circle(x, y, drill / 2), "hole", nid, 0.0, allL, tag=c)):
                c.shapes.append(s)
                self.dyn.add(s)
        for cm in list(self.g.classes.values()):
            self._mark(c, cm)
        c.state = "routed"
        self.routed.append(c)

    def _rip(self, c):
        nid = self.b.nets[c.net].id
        for key, (tl, vl, vbl) in c.marks.items():
            cm = self.g.classes[key]
            cm.unmark(False, tl, nid)
            cm.unmark(True, vl, nid)
            if vbl:
                cm.unmark(True, vbl, BLOCK)
        for s in c.shapes:
            self.dyn.remove(s)
        c.marks, c.sets, c.shapes, c.segs, c.vias, c.states = {}, {}, [], [], [], ()
        c.state = "pending"
        c.notes = []
        self.routed.remove(c)

    def rip_net(self, name):
        out = []
        for c in [c for c in self.routed if c.net == name]:
            self._rip(c)
            out.append(c)
        self.comp.pop(name, None)
        self.dp_done.pop(name, None)
        return out

    # ── components (what is already joined) ───────────────────────────────
    def _static_cells(self, name, cid):
        key = (name, cid)
        d = self.cluster_cache.get(key)
        if d is not None:
            return d
        g, b = self.g, self.b
        G, W, step = g.G, g.W, g.step
        cl = b.clusters[name][cid]
        d = {}
        for p in cl["pads"]:
            for l in p.layers:
                shapes = p.shapes.get(l) or []
                if not shapes:
                    continue
                big = max((max(s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1]) for s in shapes), default=0) > LARGE_PAD
                for s in shapes:
                    # big pads may be entered anywhere; small ones off-centre only as a (penalised) last resort
                    inset = -min(0.12, 0.3 * min(s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1]))
                    for i in g.cells(g.shape_spans(s, inset) if s.kind == "box" else g.poly_spans(s.g)):
                        d.setdefault(l * G + i, None if big else (p.x, p.y, 0))
                cx, cy = g.cell(p.x, p.y)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        x, y = g.mx(cx + dx), g.my(cy + dy)
                        if abs(x - p.x) > 0.75 * step or abs(y - p.y) > 0.75 * step:
                            continue
                        if any(s.dist_pt(x, y) <= -0.02 for s in shapes):
                            d[l * G + (cy + dy) * W + cx + dx] = (p.x, p.y)
        for net, lay, x1, y1, x2, y2, w in cl["tracks"]:
            for i in g.cells(g.capsule_spans(x1, y1, x2, y2, max(0.02, min(w / 2 - 0.03, 0.72 * step)))):
                d.setdefault(lay * G + i, (x1, y1, x2, y2))
        for net, x, y, size, drill, top, bot in cl["vias"]:
            cx, cy = g.cell(x, y)
            if math.hypot(g.mx(cx) - x, g.my(cy) - y) < size / 2 - 0.03:
                for l in range(top, bot + 1):
                    d[l * G + cy * W + cx] = (x, y)
        margin = 0.2
        for lay, pts in cl["islands"]:
            inner = set(g.cells(g.poly_spans(pts)))
            n = len(pts)
            for k in range(n):
                x1, y1 = pts[k]
                x2, y2 = pts[(k + 1) % n]
                inner.difference_update(g.cells(g.capsule_spans(x1, y1, x2, y2, margin)))
            base = lay * G
            for i in inner:
                d.setdefault(base + i, None)
        self.cluster_cache[key] = d
        return d

    def _comps(self, name):
        cs = self.comp.get(name)
        if cs is None:
            cs = {}
            for cid in self.b.clusters[name]:
                cs[cid] = {"cids": {cid}, "cells": dict(self._static_cells(name, cid))}
            self.comp[name] = cs
        return cs

    def _merge(self, name, a, b, states):
        cs = self._comps(name)
        ca, cb = cs[a], cs[b]
        if ca is not cb:
            if len(ca["cells"]) < len(cb["cells"]):
                ca, cb = cb, ca
            ca["cells"].update(cb["cells"])
            ca["cids"] |= cb["cids"]
            for cid in cb["cids"]:
                cs[cid] = ca
        for s in states:
            ca["cells"].setdefault(s, None)

    # ── per-net cost fields ───────────────────────────────────────────────
    def _moves(self, nc):
        key = nc.moves_key
        m = self.moves_cache.get(key)
        if m is None:
            g, cfg = self.g, self.cfg
            m = []
            for l in range(g.L):
                if l not in nc.layers_ok:
                    m.append(None)
                    continue
                mult = nc.layer_cost.get(l, 1.0)
                d = nc.layer_dir.get(l, "any")
                bias = 0.0 if nc.keep_short else cfg.direction_bias
                ch = mult * (1 + bias if d == "v" else 1.0)
                cv = mult * (1 + bias if d == "h" else 1.0)
                cd = mult * (1 + bias / 2 if d in ("h", "v") else 1.0)
                mv = astar.layer_moves(g.W, g.G, l, ch, cv, cd)
                if cfg.style == "90":
                    mv = tuple(x for x in mv if not (x[1] and x[2]))
                m.append(mv)
            m = tuple(m)
            self.moves_cache[key] = m
        return m

    def _halo_pads(self, name):
        b = self.b
        if b.nets[name].has_pour:
            return ()
        if self._halo is None:
            out = []
            for net in b.nets.values():
                if not net.has_pour:
                    continue
                for p in net.pads:
                    for l in p.layers:
                        if l in net.pour_layers:
                            for s in p.shapes.get(l, ()):
                                out.append((l, s))
            self._halo = tuple(out)
        return self._halo

    def _cmul(self, name, nc):
        """Cost multiplier field for this net: regions, per-net avoid rects, corridor. None = uniform."""
        regs = [r for r in self.cfg.regions if r.applies(name)]
        corridor = nc.corridor
        dpc = None
        if nc.dp_partner and nc.dp_role == "n" and self.dp_done.get(nc.dp_partner):
            dpc = self.dp_done[nc.dp_partner]
        halo = self._halo_pads(name)
        if not regs and not nc.avoid and not corridor and not dpc and not halo:
            return None
        key = (tuple(r.idx for r in regs), name if (nc.avoid or corridor or dpc) else None,
               len(dpc) if dpc else 0, bool(halo))
        cm = self.cmul_cache.get(key)
        if cm is not None:
            return cm
        g = self.g
        G, W = g.G, g.W
        if corridor or dpc:
            if corridor:
                pts, width, strict, outside = corridor["points"], corridor["width"], corridor["strict"], corridor["outside_cost"]
                lines = [pts]
            else:
                lines, width, strict, outside = dpc, 4 * (nc.dp_gap + nc.width) + 4 * g.step, False, 2.5
            cm = array("f", [-1.0 if strict else outside]) * g.N
            one = array("f", [1.0]) * W
            spans = []
            for pts in lines:
                for i in range(len(pts) - 1):
                    spans += list(g.capsule_spans(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], width / 2))
                if len(pts) == 1:
                    spans += list(g.capsule_spans(pts[0][0], pts[0][1], pts[0][0], pts[0][1], width / 2))
            if corridor:
                # the net's own pads are always reachable (a corridor guides the middle of the route)
                for p in self.b.nets[name].pads:
                    spans += list(g.capsule_spans(p.x, p.y, p.x, p.y, max(width / 2, 1.0)))
            for l in range(g.L):
                base = l * G
                for iy, i0, i1 in spans:
                    a = base + iy * W + i0
                    cm[a:a + i1 - i0 + 1] = one[:i1 - i0 + 1]
        else:
            cm = array("f", [1.0]) * g.N

        def rect_spans(rect):
            x0, y0, x1, y1 = rect
            i0, j0 = max(1, int(math.ceil(g.gx(x0) - 1e-9))), max(1, int(math.ceil(g.gy(y0) - 1e-9)))
            i1, j1 = min(W - 2, int(math.floor(g.gx(x1) + 1e-9))), min(g.H - 2, int(math.floor(g.gy(y1) + 1e-9)))
            if i0 <= i1:
                for iy in range(j0, j1 + 1):
                    yield iy, i0, i1
        for r in regs:
            for l in (r.layers if r.layers is not None else range(g.L)):
                base = l * G
                for iy, i0, i1 in rect_spans(r.rect):
                    a = base + iy * W
                    if r.forbid:
                        cm[a + i0:a + i1 + 1] = array("f", [-1.0]) * (i1 - i0 + 1)
                    else:
                        for i in range(a + i0, a + i1 + 1):
                            if cm[i] >= 0:
                                cm[i] *= r.cost
        # soft halo round pads that live on a pour: leave room for their thermal spokes / a via
        for l, shape in halo:
            base = l * G
            for iy, i0, i1 in g.shape_spans(shape, 0.6):
                a = base + iy * W
                for i in range(a + i0, a + i1 + 1):
                    v = cm[i]
                    if 0 <= v < 2.5:
                        cm[i] = v * 2.5 if v > 1.0 else 2.5
        for rect in nc.avoid:
            for l in range(g.L):
                base = l * G
                for iy, i0, i1 in rect_spans(rect):
                    a = base + iy * W
                    cm[a + i0:a + i1 + 1] = array("f", [-1.0]) * (i1 - i0 + 1)
        self.cmul_cache[key] = cm
        return cm

    # ── sources / targets ─────────────────────────────────────────────────
    def _escapes(self, pad, cm, nid, nc, hw, soft, cmul):
        """Exact off-grid stubs from a (small) pad centre to the first free grid cell in 8 directions."""
        g, b = self.g, self.b
        G, W, step = g.G, g.W, g.step
        out = []
        occ = cm.S if soft else cm.occ
        dyn = None if soft else self.dyn
        for l in pad.layers:
            if l not in nc.layers_ok:
                continue
            shapes = pad.shapes.get(l) or []
            if not shapes:
                continue
            ext = max(max(s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1]) for s in shapes)
            if ext > LARGE_PAD:
                continue
            tmax = ext / 2 + 1.6
            base = l * G
            for dx, dy in astar.DIRS:
                k = 1.0 if (dx == 0 or dy == 0) else 0.70710678
                t = step
                tries = 0
                while t <= tmax:
                    ix, iy = g.cell(pad.x + dx * k * t, pad.y + dy * k * t)
                    s = base + iy * W + ix
                    o = occ[s]
                    if (o == 0 or o == nid) and (cmul is None or cmul[s] >= 0):
                        x, y = g.mx(ix), g.my(iy)
                        if b.seg_clear(l, pad.x, pad.y, x, y, hw, nid, nc.clearance, dyn):
                            out.append((s, math.hypot(x - pad.x, y - pad.y) / step + 0.5, (pad.x, pad.y)))
                            break
                        tries += 1
                        if tries >= 3:
                            break
                    t += step
        return out

    def _endpoints(self, c, cm, nid, nc, hw, soft, cmul):
        """-> (sources {state: g0}, source anchors, targets {state: anchor}, (tx, ty))"""
        g = self.g
        G, W, step = g.G, g.W, g.step
        cs = self._comps(c.net)
        occ = cm.S if soft else cm.occ
        ok_layers = nc.layers_ok
        src, sinfo = {}, {}

        def centred(cells):
            """Pads whose centre cell is usable: their off-centre cells are not offered at all."""
            out = set()
            for s, anc in cells.items():
                if anc is not None and len(anc) == 2:
                    o = occ[s]
                    if not (o and o != nid) and (s // G) in ok_layers and not (cmul is not None and cmul[s] < 0):
                        out.add(anc)
            return out
        have = centred(cs[c.a]["cells"])
        for s, anc in cs[c.a]["cells"].items():
            o = occ[s]
            if (o and o != nid) or (s // G) not in ok_layers or (cmul is not None and cmul[s] < 0):
                continue
            if anc is not None and len(anc) == 3 and (anc[0], anc[1]) in have:
                continue
            g0 = 0.0
            if anc is not None and len(anc) == 2:
                i = s % G
                g0 = math.hypot(g.mx(i % W) - anc[0], g.my(i // W) - anc[1]) / step
            elif anc is not None and len(anc) == 3:
                i = s % G
                g0 = 2 * math.hypot(g.mx(i % W) - anc[0], g.my(i // W) - anc[1]) / step + 4.0
                anc = None
            src[s] = g0
            sinfo[s] = anc
        for s, g0, pt in self._escapes(c.pa, cm, nid, nc, hw, soft, cmul):
            if s not in src or sinfo.get(s) is None or g0 < src[s]:
                src[s] = g0
                sinfo[s] = ("esc", pt[0], pt[1])
        tgt = {}
        if c.b is None:
            mine = cs[c.a]
            seen = set()
            for cid, comp in cs.items():
                if comp is mine or id(comp) in seen:
                    continue
                seen.add(id(comp))
                have = centred(comp["cells"])
                for s, anc in comp["cells"].items():
                    if (s // G) in ok_layers:
                        if anc is not None and len(anc) == 3:
                            if (anc[0], anc[1]) in have:
                                continue
                            anc = None
                        tgt[s] = anc
            # aim at the nearest foreign pad / island corner
            best = None
            for cid, comp in cs.items():
                if comp is mine:
                    continue
                cl = self.b.clusters[c.net][cid]
                for p in cl["pads"]:
                    d = abs(p.x - c.pa.x) + abs(p.y - c.pa.y)
                    if best is None or d < best[0]:
                        best = (d, p.x, p.y)
                for _l, pts in cl["islands"]:
                    for x, y in pts[::3]:
                        d = abs(x - c.pa.x) + abs(y - c.pa.y)
                        if best is None or d < best[0]:
                            best = (d, x, y)
            tx, ty = g.cell(best[1], best[2]) if best else g.cell(c.pa.x, c.pa.y)
        else:
            have = centred(cs[c.b]["cells"])
            for s, anc in cs[c.b]["cells"].items():
                if (s // G) in ok_layers:
                    if anc is not None and len(anc) == 3:
                        if (anc[0], anc[1]) in have:
                            continue
                        anc = None
                    tgt[s] = anc
            for s, g0, pt in self._escapes(c.pb, cm, nid, nc, hw, soft, cmul):
                if tgt.get(s) is None:
                    tgt[s] = ("esc", pt[0], pt[1])
            tx, ty = g.cell(c.pb.x, c.pb.y)
        return src, sinfo, tgt, (tx, ty)

    # ── one connection ────────────────────────────────────────────────────
    def _search(self, c, nc, width, soft, via_mult=1.0):
        b, g, cfg = self.b, self.g, self.cfg
        net = b.nets[c.net]
        nid = net.id
        key = b.class_key(width, nc.clearance, nc.via, nc.drill)
        cm = self.cm_for(key)
        hw = width / 2
        cmul = self._cmul(c.net, nc)
        src, sinfo, tgt, (tx, ty) = self._endpoints(c, cm, nid, nc, hw, soft, cmul)
        if not src or not tgt:
            pad = c.pa if not src else (c.pb or c.pa)
            reason = "no_start" if not src else "no_target"
            if cmul is not None:
                ix, iy = g.cell(pad.x, pad.y)
                if any(cmul[l * g.G + iy * g.W + ix] < 0 for l in pad.layers if l in nc.layers_ok):
                    reason = "forbidden_pad"
            if not any(l in nc.layers_ok for l in pad.layers):
                reason = "layer_pad"
            return None, {"reason": reason, "where": [pad.x, pad.y], "cm": cm, "pad": pad.name}
        if self.hist is None and soft:
            self.hist = array("f", [0.0]) * g.N
        dist = max(abs(g.cell(c.pa.x, c.pa.y)[0] - tx), abs(g.cell(c.pa.x, c.pa.y)[1] - ty))
        limit = int(min(g.N, 120000 + 1500 * dist) * cfg.effort)
        self.stats["searches"] += 1
        path, n_exp, best = astar.search(
            g, cm, nid, src, tgt, tx, ty, moves=self._moves(nc), turn=self.turn,
            via_cost=nc.via_cost * via_mult / g.step, layers_ok=nc.layers_ok, cmul=cmul, hist=self.hist,
            soft=soft, soft_cost=cfg.ripup_cost / g.step, hweight=(0.6 if c.pour else cfg.hweight), limit=limit,
            via_ok=(nc.max_vias != 0 and len(nc.layers_ok) > 1))
        self.stats["expansions"] += n_exp
        if path is None:
            i = best % g.G if best >= 0 else None
            where = [round(v, 2) for v in g.xy(i)] if i is not None else [c.pa.x, c.pa.y]
            return None, {"reason": "limit" if n_exp > limit else "blocked", "where": where, "cm": cm}
        return path, {"cm": cm, "sinfo": sinfo, "tgt": tgt, "hw": hw, "key": key}

    def _geometry(self, c, nc, path, info, width, check_dyn=True):
        """Cell path -> (segs, vias, states). Ends are snapped to exact anchors when legal."""
        g, b = self.g, self.b
        G, W = g.G, g.W
        nid = b.nets[c.net].id
        hw = width / 2
        dyn = self.dyn if check_dyn else None
        runs = []          # [(layer, [(x, y), ...])]
        vias = []
        cur_l, pts = None, []
        for s in path:
            l = s // G
            i = s - l * G
            xy = (g.mx(i % W), g.my(i // W))
            if l != cur_l:
                if cur_l is not None:
                    runs.append((cur_l, pts))
                    vias.append((xy[0], xy[1], nc.via, nc.drill))
                cur_l, pts = l, [xy]
            else:
                pts.append(xy)
        runs.append((cur_l, pts))

        def snap(anc, lay, x, y):
            """exact point this end should reach, or None"""
            if anc is None:
                return None
            if anc[0] == "esc":
                return (anc[1], anc[2]), True
            if len(anc) == 4:
                px, py = geom.pt_seg_proj(x, y, *anc)
            else:
                px, py = anc
            if math.hypot(px - x, py - y) < 1e-4:
                return None
            if b.seg_clear(lay, px, py, x, y, hw, nid, nc.clearance, dyn):
                return (px, py), False
            return None
        a0 = snap(info["sinfo"].get(path[0]), runs[0][0], *runs[0][1][0])
        if a0:
            runs[0][1].insert(0, a0[0])
        a1 = snap(info["tgt"].get(path[-1]), runs[-1][0], *runs[-1][1][-1])
        if a1:
            runs[-1][1].append(a1[0])
        segs = []
        for lay, pts in runs:
            pts = _simplify(pts)
            for i in range(len(pts) - 1):
                segs.append((lay, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], width))
        return segs, vias

    def _victims(self, c, path, info, segs):
        """Nets whose routed copper the soft path runs through."""
        g = self.g
        G = g.G
        key = info["key"]
        P = set(path)
        PV = set()
        for i in range(1, len(path)):
            if path[i] // G != path[i - 1] // G:
                PV.add(path[i] % G)
        out = []
        for r in self.routed:
            if r.net == c.net or r.net in out:
                continue
            st = r.sets.get(key)
            if st is None:
                continue
            if not st[0].isdisjoint(P) or (PV and not st[1].isdisjoint(PV)):
                out.append(r.net)
        return out

    def _dyn_offenders(self, c, nc, segs, vias, width):
        """Nets of routed shapes an (off-grid) part of this geometry would violate."""
        b = self.b
        nid = b.nets[c.net].id
        out = set()
        for lay, x1, y1, x2, y2, w in segs:
            bb = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            for s in self.dyn.near(lay, bb, w / 2 + nc.clearance + 0.6):
                if s.net == nid:
                    continue
                need = w / 2 + (max(nc.clearance, s.clr) if s.cls == "cu" else b.rules["hole_clearance"])
                if s.dist_seg(x1, y1, x2, y2) < need + 0.002:
                    out.add(s.tag.net)
        return out

    def route_conn(self, c):
        b, cfg = self.b, self.cfg
        nc = cfg.net(c.net)
        widths = [nc.width]
        for w in nc.neck_widths:
            if w < widths[-1] - 1e-6:
                widths.append(w)
        fail = None
        for wi, width in enumerate(widths):
            path, info = self._search(c, nc, width, soft=False)
            if path is not None:
                ok = self._finish(c, nc, path, info, width)
                if ok:
                    if wi:
                        c.notes.append(f"necked to {width} mm (no room at {widths[0]} mm)")
                    return True
                fail = {"reason": c.reason, "where": c.where}
            elif fail is None or info["reason"] != "no_start":
                fail = info
        # rip-up and reroute
        can_rip = cfg.ripup_rounds > 0 and c.rips < cfg.ripup_rounds and not self.spent() and not c.pour
        width = widths[-1]
        if fail and fail.get("reason") in ("max_length", "max_vias"):
            can_rip = False
        path, info = self._search(c, nc, width, soft=True)
        if path is not None:
            segs, vias = self._geometry(c, nc, path, info, width, check_dyn=False)
            victims = self._victims(c, path, info, segs)
            victims += [n for n in sorted(self._dyn_offenders(c, nc, segs, vias, width)) if n not in victims]
            c.blocked_by = victims
            if can_rip and victims:
                c.rips += 1
                self.stats["ripups"] += 1
                # remember where the fight was
                occ = info["cm"].occ
                nid = b.nets[c.net].id
                hist = self.hist
                inc = cfg.history_cost / self.g.step
                for s in path:
                    o = occ[s]
                    if o and o != nid:
                        hist[s] += inc
                ripped = []
                for v in victims:
                    ripped += self.rip_net(v)
                ok = self._finish(c, nc, path, info, width, recheck=True)
                for r in ripped:
                    heappush(self.pending, (r.key, r.idx))
                if ok:
                    return True
                fail = {"reason": c.reason, "where": c.where}
        elif fail is None:
            fail = info
        c.state = "failed"
        c.reason, c.where = _reason_text(fail, c, nc, widths, c.blocked_by), fail.get("where")
        for pad in (c.pa, c.pb):
            if pad is not None and not self.b.inside(pad.x, pad.y):
                c.reason = (f"pad {pad.name} at ({round(pad.x, 2)}, {round(pad.y, 2)}) is outside the board outline — "
                            f"place the part on the board first (pcb_place)")
                c.where = [pad.x, pad.y]
                break
        return False

    def _finish(self, c, nc, path, info, width, recheck=False):
        g = self.g
        G = g.G
        if recheck:
            occ, vocc = info["cm"].occ, info["cm"].vocc
            nid = self.b.nets[c.net].id
            for i, s in enumerate(path):
                o = occ[s]
                if o and o != nid:
                    c.reason, c.where = "blocked", list(g.xy(s % G))
                    return False
        segs, vias = self._geometry(c, nc, path, info, width)
        length = sum(math.hypot(x2 - x1, y2 - y1) for _l, x1, y1, x2, y2, _w in segs)
        if nc.max_vias is not None and len(vias) > nc.max_vias:
            c.reason, c.where = "max_vias", [vias[0][0], vias[0][1]]
            c.length = len(vias)
            return False
        if nc.max_length is not None:
            done = sum(r.length for r in self.routed if r.net == c.net)
            if done + length > nc.max_length + 1e-6:
                c.reason, c.where = "max_length", [segs[0][1], segs[0][2]] if segs else None
                c.length = round(done + length, 2)
                return False
        c.segs, c.vias, c.width, c.length = segs, vias, width, length
        c.states = path
        self._commit(c)
        if c.b is None:
            # joined whichever component owns the cell we arrived at
            cs = self._comps(c.net)
            end = path[-1]
            for cid, comp in cs.items():
                if comp is not cs[c.a] and end in comp["cells"]:
                    self._merge(c.net, c.a, cid, path)
                    break
        else:
            self._merge(c.net, c.a, c.b, path)
        if self.cfg.net(c.net).dp_role == "p":
            line = []
            for lay, x1, y1, x2, y2, w in segs:
                if not line or line[-1] != (x1, y1):
                    line.append((x1, y1))
                line.append((x2, y2))
            self.dp_done.setdefault(c.net, []).append(line)
        return True

    # ── main loop ─────────────────────────────────────────────────────────
    def run(self):
        conns = self.plan_connections()
        self.pending = []
        for c in conns:
            heappush(self.pending, (c.key, c.idx))
        total = len(conns)
        done = 0
        last = 0.0
        while self.pending:
            _k, i = heappop(self.pending)
            c = conns[i]
            if c.state != "pending":
                continue
            if self.spent():
                c.state, c.reason = "failed", "time budget used up before this connection was tried"
                c.where = [c.pa.x, c.pa.y]
                continue
            self.route_conn(c)
            done += 1
            now = time.time()
            if self.progress and now - last > 0.5:
                last = now
                n_ok = len(self.routed)
                self.progress(min(0.99, n_ok / max(1, total)), f"routing: {n_ok}/{total} connections ({c.net})")
        # second chance: space freed by later rip-ups
        for c in conns:
            if c.state == "failed" and not self.spent() and "time budget" not in (c.reason or ""):
                why, where, blk = c.reason, c.where, c.blocked_by
                c.state = "pending"
                saved, self.cfg.ripup_rounds = self.cfg.ripup_rounds, 0
                try:
                    if not self.route_conn(c):
                        c.reason, c.where, c.blocked_by = why, where, blk or c.blocked_by
                finally:
                    self.cfg.ripup_rounds = saved
        self._verify()
        return self.result()

    def _verify(self):
        """Exact re-check of every routed segment against static copper and the other routed nets.
        Anything that fails is taken out and reported, never written."""
        b = self.b
        for c in list(self.routed):
            nid = b.nets[c.net].id
            nc = self.cfg.net(c.net)
            bad = None
            for lay, x1, y1, x2, y2, w in c.segs:
                if not b.seg_clear(lay, x1, y1, x2, y2, w / 2, nid, nc.clearance, self.dyn):
                    bad = [round((x1 + x2) / 2, 2), round((y1 + y2) / 2, 2)]
                    break
            if bad:
                for r in self.rip_net(c.net):
                    r.state = "failed"
                    r.reason = "internal clearance self-check failed — connection dropped rather than written"
                    r.where = bad

    def result(self):
        b = self.b
        tracks, vias, owners_t, owners_v = [], [], [], []
        for c in self.routed:
            for lay, x1, y1, x2, y2, w in c.segs:
                if math.hypot(x2 - x1, y2 - y1) < 1e-5:
                    continue
                tracks.append([c.net, b.layers[lay], round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4), round(w, 4)])
                owners_t.append(c.idx)
            for x, y, size, drill in c.vias:
                vias.append([c.net, round(x, 4), round(y, 4), size, drill])
                owners_v.append(c.idx)
        return {"tracks": tracks, "vias": vias, "owners_t": owners_t, "owners_v": owners_v,
                "conns": self.conns, "skipped": self.skipped, "stats": dict(self.stats), "work": self.work(),
                "clock_hit": self.clock_hit,
                "seconds": round(time.time() - self.t0, 2)}


def _simplify(pts):
    """Drop repeated and collinear points."""
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 1e-6 and abs(out[-1][1] - p[1]) < 1e-6:
            continue
        while len(out) >= 2:
            ax, ay = out[-2]
            bx, by = out[-1]
            cross = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
            dot = (bx - ax) * (p[0] - bx) + (by - ay) * (p[1] - by)
            if abs(cross) < 1e-7 and dot > 0:
                out.pop()
            else:
                break
        out.append(p)
    return out


def _reason_text(fail, c, nc, widths, blocked_by):
    r = (fail or {}).get("reason") or "blocked"
    w = widths[-1]
    if r == "forbidden_pad":
        return (f"pad {fail.get('pad')} lies inside a forbidden region / avoid rectangle (or outside its strict corridor): "
                f"the net cannot leave it — add the net to regions[].except, or use cost instead of forbid")
    if r == "layer_pad":
        return (f"pad {fail.get('pad')} is not on any layer this net may use "
                f"({', '.join(str(l) for l in sorted(nc.layers_ok))}) — widen nets[...].layers / global.layers")
    if r == "no_start":
        return (f"pad {c.pa.name} is boxed in: no free grid cell next to it for a {w} mm track "
                f"(clearance {nc.clearance} mm) on the allowed layers")
    if r == "no_target":
        return f"nothing reachable to connect to on the allowed layers (target {c.pb.name if c.pb else 'pour'})"
    if r == "max_vias":
        return f"needs {int(c.length)} vias but max_vias={nc.max_vias}"
    if r == "max_length":
        return f"shortest route found makes the net {c.length} mm long, over max_length={nc.max_length}"
    if r == "limit":
        return "search effort limit reached before a path was found (raise global.effort or time_budget_s)"
    if blocked_by:
        return "blocked by routed nets: " + ", ".join(blocked_by[:6])
    if nc.corridor and nc.corridor["strict"]:
        return ("no path inside the strict corridor — widen corridor_width, move its points, or set "
                "corridor_strict=false (leaving the corridor then only costs 3x)")
    return "no path: walled in by pads / keep-outs / board edge / forbidden regions"
